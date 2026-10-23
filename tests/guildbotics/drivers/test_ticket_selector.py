"""The host selects ticket work and settles how its run ended.

Every route that runs the ticket workflow goes through ``TicketSelector``; the
workflow only runs the AI CLI turn with the input selected here.
"""

import pytest

from guildbotics.capabilities.task_runs import TaskRunStore
from guildbotics.drivers.ticket_selector import TicketSelector
from guildbotics.entities.task import Task
from guildbotics.runtime.member_invocation import Work
from guildbotics.runtime.port import ServiceUnavailable, WorkflowStatus
from guildbotics.runtime.workflow_invocation import WorkflowInvocation
from guildbotics.utils.correlation import current_trace, trace_scope
from guildbotics.utils.i18n_tool import get_language, set_language, t
from tests.guildbotics.local_code_host import (
    TRACKER,
    comment,
    item,
    local_member,
    pull_request,
    ticket,
)

ISSUE_URL = f"local://tickets/{TRACKER}/T-1"


@pytest.fixture(autouse=True)
def _english():
    previous_language = get_language()
    set_language("en")
    yield
    set_language(previous_language)


@pytest.fixture(autouse=True)
def _talk_as_echoes(monkeypatch):
    async def fake_talk_as(context, text, role, attachments):
        return text

    monkeypatch.setattr("guildbotics.intelligences.functions.talk_as", fake_talk_as)


class _Context:
    """The member's local code host and board, closed per selection step."""

    def __init__(self) -> None:
        self.member = local_member()
        self.person = self.member.person
        self.team = self.member.team
        self.closed = 0

    def clone_for(self, person: object) -> _Context:
        return self

    def get_ticket_service(self):
        return self.member.tickets

    def get_code_hosting_service(self):
        return self.member.code

    async def aclose(self) -> None:
        self.closed += 1


def _task(status: str = Task.READY, number: int = 1) -> Task:
    url = ticket(number, title="Fix login", lane=status, assignees=["aiko"])
    return Task(id=url, title="Fix login", description="", status=status, url=url)


def _statuses(url: str = ISSUE_URL) -> list[WorkflowStatus]:
    """The statuses the comments the run left on the item carry."""
    return [
        status
        for c in item(url)["comments"]
        if (status := WorkflowStatus.from_payload(c.get("status"))) is not None
    ]


def _moves(monkeypatch, context: _Context) -> list[tuple[str, str]]:
    """The moves the selector makes on the board, each made."""
    moves: list[tuple[str, str]] = []
    move = context.member.tickets.move

    async def recorded(task: Task, lane):
        moves.append((task.url or "", lane))
        return await move(task, lane)

    monkeypatch.setattr(context.member.tickets, "move", recorded)
    return moves


def _invocation(task: Task, source: str = "routine") -> WorkflowInvocation:
    return WorkflowInvocation(
        command="workflows/ticket_driven_workflow",
        person_id="aiko",
        source=source,  # type: ignore[arg-type]
        trigger_type="ticket",
        payload={
            "task": task.model_dump(),
            "ticket_url": ISSUE_URL,
            "pull_request_url": "",
            "trigger_reason": "",
        },
        work=Work.of_ticket(ISSUE_URL),
    )


def _rate_limit_error(at="2026-07-04T11:44:00+09:00", hint="11:44 AM"):
    from guildbotics.intelligences.agent_runtime.models import (
        CliAgentExecutionError,
        CliAgentExecutionResult,
    )

    return CliAgentExecutionError(
        cli_agent="codex",
        result=CliAgentExecutionResult(
            stdout="",
            stderr="rate limit",
            returncode=75,
            error_category="rate_limited",
            error_details={
                "retry_after_at": at,
                "retry_after_text": hint,
            },
        ),
    )


def _capture_rate_limit_events(monkeypatch) -> list[dict]:
    recorded: list[dict] = []
    monkeypatch.setattr(
        "guildbotics.capabilities.workflow_rate_limits.record_correlated_event",
        lambda **kwargs: recorded.append(kwargs),
    )
    return recorded


def _failing(error: Exception):
    async def run_workflow(invocation: WorkflowInvocation):
        raise error

    return run_workflow


@pytest.mark.asyncio
async def test_run_moves_a_ready_ticket_and_hands_the_turn_its_run(monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_TICKET_MAX_ATTEMPTS", "3")
    context = _Context()
    moves = _moves(monkeypatch, context)
    task = _task()
    seen: list[tuple[WorkflowInvocation, dict[str, object]]] = []

    async def run_workflow(invocation: WorkflowInvocation) -> str:
        trace = current_trace()
        assert trace is not None
        seen.append((invocation, dict(trace.attributes)))
        return "done"

    with trace_scope("manual", trace_id="trace-7", person_id="aiko"):
        result = await TicketSelector(context).run(  # type: ignore[arg-type]
            context.person, _invocation(task), run_workflow
        )

    assert result == "done"
    assert moves == [(ISSUE_URL, "in_progress")]
    assert item(ISSUE_URL)["lane"] == Task.IN_PROGRESS
    assert item(ISSUE_URL)["comments"] == []
    invocation, attributes = seen[0]
    # The run is its trace, the work the ticket the host selected, and the
    # host decides the completion budget.
    assert invocation.run_id == "trace-7"
    assert invocation.work == Work.of_ticket(ISSUE_URL)
    assert invocation.payload["max_completion_attempts"] == 3
    assert invocation.payload["ticket_url"] == ISSUE_URL
    # A route whose trace opened before selection still names the ticket.
    assert attributes["target.title"] == "Fix login"
    assert attributes["target.url"] == ISSUE_URL
    assert context.closed == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_trace")
@pytest.mark.parametrize(("raw", "expected"), [("", 5), ("0", 1), ("x", 5)])
async def test_run_hands_the_turn_the_default_budget(monkeypatch, raw, expected):
    monkeypatch.setenv("GUILDBOTICS_TICKET_MAX_ATTEMPTS", raw)
    context = _Context()
    moves = _moves(monkeypatch, context)
    invocations: list[WorkflowInvocation] = []

    async def run_workflow(invocation: WorkflowInvocation) -> None:
        invocations.append(invocation)

    await TicketSelector(context).run(  # type: ignore[arg-type]
        context.person, _invocation(_task(Task.IN_PROGRESS)), run_workflow
    )

    assert invocations[0].run_id == "trace"
    assert invocations[0].payload["max_completion_attempts"] == expected
    # Only a ticket that is ready moves to the working lane.
    assert moves == []


@pytest.mark.asyncio
async def test_a_run_outside_a_trace_is_refused_before_the_ticket_moves():
    """The run is its trace, and every route that takes a ticket opens one
    first: outside any, the ticket stays where it is and no turn runs."""
    context = _Context()
    task = _task()

    async def run_workflow(invocation: WorkflowInvocation) -> None:
        raise AssertionError("no trace, no turn")

    with pytest.raises(RuntimeError, match="outside a trace"):
        await TicketSelector(context).run(  # type: ignore[arg-type]
            context.person, _invocation(task), run_workflow
        )

    assert item(ISSUE_URL)["lane"] == Task.READY
    assert item(ISSUE_URL)["comments"] == []


@pytest.mark.asyncio
async def test_failed_run_posts_a_safe_status_comment_and_raises():
    context = _Context()
    task = _task()
    error = RuntimeError("codex failed: secret-token-123 /home/aiko/run.log")

    with (
        trace_scope("routine", trace_id="trace-9", person_id="aiko"),
        pytest.raises(RuntimeError) as raised,
    ):
        await TicketSelector(context).run(  # type: ignore[arg-type]
            context.person, _invocation(task), _failing(error)
        )

    assert raised.value is error
    [comment] = item(ISSUE_URL)["comments"]
    assert "secret-token-123" not in comment["body"]
    assert "RuntimeError" not in comment["body"]
    assert ".log" not in comment["body"]
    assert _statuses() == [
        WorkflowStatus(
            reason="failed", person_id="aiko", run_id="trace-9", subject=ISSUE_URL
        )
    ]


@pytest.mark.asyncio
async def test_a_failed_pull_request_run_says_so_on_the_pull_request():
    """The status goes where the work came from: a pull request's on the code
    host, not on the ticket service."""
    context = _Context()
    ticket_url = _task()
    pull = pull_request(
        2, "feature", author="aiko", comments=[comment("reviewer", "please fix")]
    )
    selector = TicketSelector(context)  # type: ignore[arg-type]
    [candidate, _] = await selector.candidates(context.person)  # type: ignore[arg-type]
    invocation = await selector.refresh(context.person, candidate)  # type: ignore[arg-type]
    assert invocation is not None

    with (
        trace_scope("routine", trace_id="trace-4", person_id="aiko"),
        pytest.raises(RuntimeError),
    ):
        await selector.run(  # type: ignore[arg-type]
            context.person, invocation, _failing(RuntimeError("boom"))
        )

    assert _statuses(pull) == [
        WorkflowStatus(
            reason="failed", person_id="aiko", run_id="trace-4", subject=pull
        )
    ]
    assert item(ticket_url.url or "")["comments"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_trace")
async def test_failed_move_is_reported_like_a_failed_run(monkeypatch):
    context = _Context()
    task = _task()

    async def failing_move(task: Task, lane: str) -> bool:
        raise RuntimeError("project board is down")

    monkeypatch.setattr(context.member.tickets, "move", failing_move)
    called: list[WorkflowInvocation] = []

    async def run_workflow(invocation: WorkflowInvocation) -> None:
        called.append(invocation)

    with pytest.raises(RuntimeError, match="project board"):
        await TicketSelector(context).run(  # type: ignore[arg-type]
            context.person, _invocation(task), run_workflow
        )

    assert called == []
    assert len(_statuses()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["routine", "scheduled", "manual"])
async def test_an_unavailable_move_is_deferred_only_for_autonomous_work(
    monkeypatch, source
):
    context = _Context()
    task = _task()

    async def unavailable(*_args):
        raise ServiceUnavailable("not connected")

    monkeypatch.setattr(context.member.tickets, "move", unavailable)
    with trace_scope(source, trace_id="unavailable-move", person_id="aiko"):
        with pytest.raises(ServiceUnavailable):
            await TicketSelector(context, source=source).run(
                context.person, _invocation(task, source), unavailable
            )
    assert item(ISSUE_URL)["lane"] == Task.READY
    assert [status.reason for status in _statuses()] == (
        ["failed"] if source == "manual" else []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("notice_language", ["en"], indirect=True)
@pytest.mark.parametrize(
    ("source", "at", "hint", "suffix"),
    [
        *(
            (source, "2026-07-04T02:44:00Z", "11:44 AM", "_with_reset")
            for source in ["routine", "scheduled", "manual"]
        ),
        ("routine", "", "Resets in 1h", "_with_hint"),
        ("routine", "", "", ""),
    ],
)
async def test_rate_limited_run_is_settled_with_a_status_comment_and_an_event(
    monkeypatch, source, notice_language, at, hint, suffix, local_rate_limit_timezone
):
    context = _Context()
    task = _task()
    recorded = _capture_rate_limit_events(monkeypatch)

    with trace_scope(source, trace_id="trace-3", person_id="aiko"):
        result = await TicketSelector(context).run(  # type: ignore[arg-type]
            context.person,
            _invocation(task, source),
            _failing(_rate_limit_error(at, hint)),
        )

    # Settled, not raised: the status comment keeps the ticket out of
    # selection until the reset, so the worker does not count an error. A
    # manual run shows the same notice.
    [comment] = item(ISSUE_URL)["comments"]
    assert result == t(
        f"commands.workflows.common.rate_limited_escalation{suffix}",
        retry_after="2026-07-04 11:44:00+09:00" if at else hint,
        retry_guidance=t(
            "commands.workflows.common.rate_limited_retry_at"
            if at
            else "commands.workflows.common.rate_limited_retry_ticket"
        ),
    )
    assert result in comment["body"]
    [status] = _statuses()
    assert (status.reason, status.run_id, status.subject, status.retry_after_text) == (
        "rate_limited",
        "trace-3",
        ISSUE_URL,
        hint,
    )
    [event] = recorded
    assert event["event_type"] == "workflow.rate_limited"
    assert event["default_source"] == source
    assert event["command"] == "workflows/ticket_driven_workflow"
    assert event["payload"]["run_id"] == "trace-3"
    assert event["payload"]["subject_id"] == ISSUE_URL
    assert event["attributes"]["rate_limit.retry_after_at"] == at
    assert status.retry_after_at == at


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_trace")
async def test_rate_limit_is_recorded_even_if_the_comment_cannot_be_posted(
    monkeypatch,
):
    context = _Context()
    task = _task()

    async def failing_comment(*_args, **_kwargs):
        raise RuntimeError("the code host is down")

    monkeypatch.setattr(context.member.tickets, "comment", failing_comment)
    recorded = _capture_rate_limit_events(monkeypatch)

    result = await TicketSelector(context).run(  # type: ignore[arg-type]
        context.person, _invocation(task), _failing(_rate_limit_error())
    )

    assert result
    assert [event["event_type"] for event in recorded] == ["workflow.rate_limited"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_trace")
async def test_run_next_runs_the_first_ticket_in_patrol_order():
    context = _Context()
    first = _task(number=1)
    _task(number=2)
    seen: list[WorkflowInvocation] = []

    async def run_workflow(invocation: WorkflowInvocation) -> str:
        seen.append(invocation)
        return "done"

    selector = TicketSelector(context, source="manual")  # type: ignore[arg-type]
    result = await selector.run_next(context.person, run_workflow)  # type: ignore[arg-type]

    assert result == "done"
    [invocation] = seen
    assert invocation.source == "manual"
    assert invocation.payload["task"]["id"] == first.id
    # The work is fixed when the ticket is selected: its URL, its subject.
    assert invocation.work == Work.of_ticket(invocation.payload["ticket_url"])
    assert item(first.id or "")["lane"] == Task.IN_PROGRESS
    assert item(f"local://tickets/{TRACKER}/T-2")["lane"] == Task.READY


@pytest.mark.asyncio
async def test_run_next_without_work_runs_nothing():
    async def run_workflow(invocation: WorkflowInvocation) -> None:
        raise AssertionError("no ticket, no workflow")

    context = _Context()
    selector = TicketSelector(context)  # type: ignore[arg-type]

    assert await selector.run_next(context.person, run_workflow) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_candidates_are_the_pull_requests_and_then_the_board_and_refresh_by_kind():
    """The patrol takes up the pull requests it answers for before new tickets,
    and re-reads each candidate from the service it came from."""
    context = _Context()
    ready = _task()
    pull = pull_request(
        2, "ticket/T-1", author="aiko", comments=[comment("reviewer", "please fix")]
    )
    selector = TicketSelector(context)  # type: ignore[arg-type]

    candidates = await selector.candidates(context.person)  # type: ignore[arg-type]

    assert [(task.url, task.trigger_reason) for task in candidates] == [
        (pull, "pull_request_feedback"),
        (ready.url, "ready_lane"),
    ]
    refreshed = [
        await selector.refresh(context.person, task)  # type: ignore[arg-type]
        for task in candidates
    ]
    # The run's ticket URL is the candidate's own URL, a pull request's
    # included.
    assert [
        (r.payload["ticket_url"], r.payload["pull_request_url"], r.work)
        for r in refreshed
        if r is not None
    ] == [
        (pull, pull, Work.of_ticket(pull)),
        (ready.url, "", Work.of_ticket(ready.url or "")),
    ]


def test_ticket_trace_attributes_for_ticket_and_pull_request():
    ticket_task = Task(id="1", title="T", description="D", url=ISSUE_URL)
    assert ticket_task.trace_attributes() == {
        "target.provider": "",
        "target.label": "",
        "target.kind": "ticket",
        "target.title": "T",
        "target.url": ISSUE_URL,
    }

    pr_url = "local://code/acme/team/demo/pulls/7"
    pr = Task(id="2", title="T", description="D", url=pr_url, pull_request_url=pr_url)
    assert pr.trace_attributes() == {
        "target.provider": "",
        "target.label": "",
        "target.kind": "pull_request",
        "target.title": "T",
        "target.url": pr_url,
    }


def _checks_invocation(pull: str, trigger: str) -> WorkflowInvocation:
    task = Task(
        id=pull,
        url=pull,
        title="Pull request 2",
        description="",
        status=Task.IN_PROGRESS,
        pull_request_url=pull,
        trigger_reason=trigger,
    )
    return WorkflowInvocation(
        command="workflows/ticket_driven_workflow",
        person_id="aiko",
        source="routine",
        trigger_type="ticket",
        payload={
            "task": task.model_dump(),
            "ticket_url": pull,
            "pull_request_url": pull,
            "trigger_reason": trigger,
        },
        work=Work.of_ticket(pull),
    )


def _completing(status: str, awaited: tuple[str, ...] = ()):
    """A run whose member completes with ``status``, awaiting the checks of
    the PRs ``awaited``."""

    async def run_workflow(invocation: WorkflowInvocation) -> str:
        store = TaskRunStore()
        store.append_evidence(invocation.run_id, "pr_comment", {"id": "comment-1"})
        for url in awaited:
            store.append_evidence(
                invocation.run_id, "checks_awaited", {"target": {"url": url}}
            )
        store.complete(
            invocation.run_id, status, "summary", invocation.work.identity, "aiko"
        )
        return status

    return run_workflow


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger", "status", "awaits", "settled"),
    [
        ("pull_request_checks", "done", (), True),
        ("pull_request_checks", "asking", (), True),
        ("pull_request_checks", "awaiting_checks", ("resumed",), False),
        ("pull_request_checks", "awaiting_checks", ("resumed", "other"), False),
        # The run awaits another PR only: the resumed PR's wait is over.
        ("pull_request_checks", "awaiting_checks", ("other",), True),
        ("pull_request_feedback", "done", (), False),
    ],
)
async def test_a_run_resumed_for_checks_ends_their_wait(
    trigger, status, awaits, settled
):
    """Whatever the resumed run wrote on the PR, the host ends the wait with
    a notice of its own, unless the run awaits that PR's checks again."""
    context = _Context()
    pull = pull_request(2, "feature", author="aiko")
    other = pull_request(3, "other", author="aiko")
    awaited = tuple({"resumed": pull, "other": other}[name] for name in awaits)
    selector = TicketSelector(context)  # type: ignore[arg-type]

    with trace_scope("routine", trace_id="trace-5", person_id="aiko"):
        result = await selector.run(  # type: ignore[arg-type]
            context.person,
            _checks_invocation(pull, trigger),
            _completing(status, awaited),
        )

    assert result == status
    assert _statuses(pull) == (
        [
            WorkflowStatus(
                reason="checks_settled",
                person_id="aiko",
                run_id="trace-5",
                subject=pull,
            )
        ]
        if settled
        else []
    )
