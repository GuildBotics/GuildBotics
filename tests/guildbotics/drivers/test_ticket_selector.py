"""The host selects ticket work and settles how its run ended.

Every route that runs the ticket workflow goes through ``TicketSelector``; the
workflow only runs the AI CLI turn with the input selected here.
"""

import pytest

from guildbotics.drivers.ticket_selector import TicketSelector
from guildbotics.entities.task import Task
from guildbotics.runtime.member_invocation import Work
from guildbotics.runtime.workflow_invocation import WorkflowInvocation
from guildbotics.utils.correlation import current_trace, trace_scope
from guildbotics.utils.i18n_tool import get_language, set_language, t
from tests.guildbotics.local_code_host import (
    comment,
    issue,
    item,
    local_member,
    pull_request,
)

ISSUE_URL = "local://owner/repo/issues/1"


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

    def clone_for(self, person: object) -> "_Context":
        return self

    def get_ticket_manager(self):
        return self.member.board

    def get_code_hosting_service(self):
        return self.member.code

    async def aclose(self) -> None:
        self.closed += 1


def _task(status: str = Task.READY, number: int = 1) -> Task:
    url = issue(number, title="Fix login", lane=status, assignees=["aiko"])
    return Task(
        id=url,
        title="Fix login",
        description="",
        status=status,
        repository="repo",
        number=number,
        url=url,
    )


def _statuses(url: str = ISSUE_URL) -> list[dict]:
    """The status comments the run left on the ticket."""
    return [c["status"] for c in item(url)["comments"] if c.get("status")]


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
    assert attributes["github.title"] == "Fix login"
    assert attributes["github.url"] == ISSUE_URL
    assert context.closed == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_trace")
@pytest.mark.parametrize(("raw", "expected"), [("", 5), ("0", 1), ("x", 5)])
async def test_run_hands_the_turn_the_default_budget(monkeypatch, raw, expected):
    monkeypatch.setenv("GUILDBOTICS_TICKET_MAX_ATTEMPTS", raw)
    context = _Context()
    invocations: list[WorkflowInvocation] = []

    async def run_workflow(invocation: WorkflowInvocation) -> None:
        invocations.append(invocation)

    await TicketSelector(context).run(  # type: ignore[arg-type]
        context.person, _invocation(_task(Task.IN_PROGRESS)), run_workflow
    )

    assert invocations[0].run_id == "trace"
    assert invocations[0].payload["max_completion_attempts"] == expected
    # Only a ticket that is ready moves to the working lane.
    assert item(ISSUE_URL)["lane"] == Task.IN_PROGRESS


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
    status = comment["status"]
    assert (
        status["reason"],
        status["person_id"],
        status["run_id"],
        status["subject_id"],
    ) == ("failed", "aiko", "trace-9", ISSUE_URL)


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_trace")
async def test_failed_move_is_reported_like_a_failed_run(monkeypatch):
    context = _Context()
    task = _task()

    async def failing_move(task: Task, status: str) -> bool:
        raise RuntimeError("project board is down")

    monkeypatch.setattr(context.member.board, "move_ticket", failing_move)
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
@pytest.mark.parametrize("notice_language", ["en", "ja"], indirect=True)
@pytest.mark.parametrize(
    "at, hint, suffix",
    [
        ("2026-07-04T02:44:00Z", "11:44 AM", "_with_reset"),
        ("", "Resets in 1h", "_with_hint"),
        ("", "", ""),
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
    status = comment["status"]
    assert (
        status["reason"],
        status["run_id"],
        status.get("retry_after_text", ""),
    ) == ("rate_limited", "trace-3", hint)
    [event] = recorded
    assert event["event_type"] == "workflow.rate_limited"
    assert event["default_source"] == source
    assert event["command"] == "workflows/ticket_driven_workflow"
    assert event["payload"]["run_id"] == "trace-3"
    assert event["payload"]["subject_id"] == ISSUE_URL
    assert event["attributes"]["rate_limit.retry_after_at"] == at
    assert status.get("retry_after_at", "") == at


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_trace")
async def test_rate_limit_is_recorded_even_if_the_comment_cannot_be_posted(
    monkeypatch,
):
    context = _Context()
    task = _task()

    async def failing_comment(*_args, **_kwargs):
        raise RuntimeError("the code host is down")

    monkeypatch.setattr(context.member.code, "comment", failing_comment)
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
    assert item("local://owner/repo/issues/2")["lane"] == Task.READY


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
    ticket = _task()
    pull = pull_request(
        2, "ticket/1", author="aiko", comments=[comment("reviewer", "please fix")]
    )
    selector = TicketSelector(context)  # type: ignore[arg-type]

    candidates = await selector.candidates(context.person)  # type: ignore[arg-type]

    assert [(task.url, task.trigger_reason) for task in candidates] == [
        (pull, "pull_request_feedback"),
        (ticket.url, "ready_lane"),
    ]
    refreshed = [
        await selector.refresh(context.person, task)  # type: ignore[arg-type]
        for task in candidates
    ]
    assert [r.payload["ticket_url"] for r in refreshed if r is not None] == [
        pull,
        ticket.url,
    ]


def test_ticket_trace_attributes_for_issue_and_pull_request():
    issue = Task(
        id="1",
        title="T",
        description="D",
        repository="repo",
        number=42,
        url="https://github.com/owner/repo/issues/42",
    )
    assert issue.trace_attributes() == {
        "github.repo": "repo",
        "github.title": "T",
        "github.kind": "issue",
        "github.url": "https://github.com/owner/repo/issues/42",
        "github.number": "42",
    }

    pr = Task(
        id="2",
        title="T",
        description="D",
        repository="repo",
        number=7,
        pull_request_url="https://github.com/owner/repo/pull/7",
    )
    assert pr.trace_attributes() == {
        "github.repo": "repo",
        "github.title": "T",
        "github.kind": "pull_request",
        "github.url": "https://github.com/owner/repo/pull/7",
        "github.number": "7",
    }
