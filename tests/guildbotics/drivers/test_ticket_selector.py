"""The host selects ticket work and settles how its run ended.

Every route that runs the ticket workflow goes through ``TicketSelector``; the
workflow only runs the AI CLI turn with the input selected here.
"""

import pytest

from guildbotics.drivers.ticket_selector import TicketSelector
from guildbotics.entities.task import Task
from guildbotics.integrations.workflow_status_comment import (
    parse_workflow_status_comment,
)
from guildbotics.observability import current_trace, trace_scope
from guildbotics.runtime.workflow_invocation import WorkflowInvocation
from guildbotics.utils.i18n_tool import get_language, set_language

ISSUE_URL = "https://github.com/o/r/issues/1"


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


class _TicketManager:
    def __init__(self, tasks: list[Task] | None = None) -> None:
        self.tasks = tasks or []
        self.moved: list[tuple[str | None, str]] = []
        self.comments: list[str] = []

    async def get_task_candidates(self) -> list[Task]:
        return list(self.tasks)

    async def refresh_task(self, task: Task) -> Task | None:
        return task

    async def get_ticket_url(self, task: Task, markdown: bool = True) -> str:
        return task.url or ISSUE_URL

    async def move_ticket(self, task: Task, status: str) -> bool:
        self.moved.append((task.id, status))
        return True

    async def add_comment_to_ticket(self, task: Task, message: str) -> None:
        self.comments.append(message)


class _Person:
    person_id = "aiko"


class _Context:
    def __init__(self, ticket_manager: _TicketManager) -> None:
        self.person = _Person()
        self.ticket_manager = ticket_manager
        self.closed = 0

    def clone_for(self, person: object) -> "_Context":
        return self

    def get_ticket_manager(self) -> _TicketManager:
        return self.ticket_manager

    async def aclose(self) -> None:
        self.closed += 1


def _task(status: str = Task.READY) -> Task:
    return Task(
        id="1",
        title="Fix login",
        description="",
        status=status,
        repository="o/r",
        number=1,
        url=ISSUE_URL,
    )


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
    )


def _rate_limit_error():
    from guildbotics.intelligences.brains.cli_agent import (
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
                "retry_after_at": "2026-07-04T11:44:00+09:00",
                "retry_after_text": "11:44 AM",
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
    manager = _TicketManager()
    context = _Context(manager)
    seen: list[tuple[WorkflowInvocation, dict[str, object]]] = []

    async def run_workflow(invocation: WorkflowInvocation) -> str:
        trace = current_trace()
        assert trace is not None
        seen.append((invocation, dict(trace.attributes)))
        return "done"

    with trace_scope("manual", trace_id="trace-7", person_id="aiko"):
        result = await TicketSelector(context).run(  # type: ignore[arg-type]
            _Person(), _invocation(_task()), run_workflow
        )

    assert result == "done"
    assert manager.moved == [("1", Task.IN_PROGRESS)]
    assert manager.comments == []
    invocation, attributes = seen[0]
    # The run is its trace, and the host decides the completion budget.
    assert invocation.payload["run_id"] == "trace-7"
    assert invocation.payload["max_completion_attempts"] == 3
    assert invocation.payload["ticket_url"] == ISSUE_URL
    # A route whose trace opened before selection still names the ticket.
    assert attributes["github.title"] == "Fix login"
    assert attributes["github.url"] == ISSUE_URL
    assert context.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw", "expected"), [("", 5), ("0", 1), ("x", 5)])
async def test_run_outside_a_trace_stands_alone_with_the_default_budget(
    monkeypatch, raw, expected
):
    monkeypatch.setenv("GUILDBOTICS_TICKET_MAX_ATTEMPTS", raw)
    manager = _TicketManager()
    payloads: list[dict] = []

    async def run_workflow(invocation: WorkflowInvocation) -> None:
        payloads.append(invocation.payload)

    await TicketSelector(_Context(manager)).run(  # type: ignore[arg-type]
        _Person(), _invocation(_task(Task.IN_PROGRESS)), run_workflow
    )

    assert payloads[0]["run_id"]
    assert payloads[0]["max_completion_attempts"] == expected
    # Only a ticket that is ready moves to the working lane.
    assert manager.moved == []


@pytest.mark.asyncio
async def test_failed_run_posts_a_safe_status_comment_and_raises():
    manager = _TicketManager()
    error = RuntimeError("codex failed: secret-token-123 /home/aiko/run.log")

    with (
        trace_scope("routine", trace_id="trace-9", person_id="aiko"),
        pytest.raises(RuntimeError) as raised,
    ):
        await TicketSelector(_Context(manager)).run(  # type: ignore[arg-type]
            _Person(), _invocation(_task()), _failing(error)
        )

    assert raised.value is error
    [comment] = manager.comments
    assert "secret-token-123" not in comment
    assert "RuntimeError" not in comment
    assert ".log" not in comment
    status = parse_workflow_status_comment(comment)
    assert status is not None
    assert (status.reason, status.person_id, status.run_id, status.subject_id) == (
        "failed",
        "aiko",
        "trace-9",
        ISSUE_URL,
    )


@pytest.mark.asyncio
async def test_failed_move_is_reported_like_a_failed_run():
    class _FailingMove(_TicketManager):
        async def move_ticket(self, task: Task, status: str) -> bool:
            raise RuntimeError("project board is down")

    manager = _FailingMove()
    called: list[WorkflowInvocation] = []

    async def run_workflow(invocation: WorkflowInvocation) -> None:
        called.append(invocation)

    with pytest.raises(RuntimeError, match="project board"):
        await TicketSelector(_Context(manager)).run(  # type: ignore[arg-type]
            _Person(), _invocation(_task()), run_workflow
        )

    assert called == []
    assert len(manager.comments) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["routine", "scheduled", "manual"])
async def test_rate_limited_run_is_settled_with_a_status_comment_and_an_event(
    monkeypatch, source
):
    manager = _TicketManager()
    recorded = _capture_rate_limit_events(monkeypatch)

    with trace_scope(source, trace_id="trace-3", person_id="aiko"):
        result = await TicketSelector(_Context(manager)).run(  # type: ignore[arg-type]
            _Person(), _invocation(_task(), source), _failing(_rate_limit_error())
        )

    # Settled, not raised: the status comment keeps the ticket out of
    # selection until the reset, so the worker does not count an error. A
    # manual run shows the same notice.
    [comment] = manager.comments
    assert result == (
        "AI CLI tool is currently rate-limited, so this workflow cannot continue "
        "now. Reset: 11:44 AM. It will retry automatically at or after this time."
    )
    assert result in comment
    status = parse_workflow_status_comment(comment)
    assert status is not None
    assert (status.reason, status.run_id, status.retry_after_text) == (
        "rate_limited",
        "trace-3",
        "11:44 AM",
    )
    [event] = recorded
    assert event["event_type"] == "workflow.rate_limited"
    assert event["default_source"] == source
    assert event["command"] == "workflows/ticket_driven_workflow"
    assert event["payload"]["run_id"] == "trace-3"
    assert event["payload"]["subject_id"] == ISSUE_URL
    assert event["attributes"]["rate_limit.retry_after_at"] == (
        "2026-07-04T11:44:00+09:00"
    )


@pytest.mark.asyncio
async def test_rate_limit_is_recorded_even_if_the_comment_cannot_be_posted(
    monkeypatch,
):
    class _FailingComment(_TicketManager):
        async def add_comment_to_ticket(self, task: Task, message: str) -> None:
            raise RuntimeError("GitHub is down")

    recorded = _capture_rate_limit_events(monkeypatch)

    result = await TicketSelector(_Context(_FailingComment())).run(  # type: ignore[arg-type]
        _Person(), _invocation(_task()), _failing(_rate_limit_error())
    )

    assert result
    assert [event["event_type"] for event in recorded] == ["workflow.rate_limited"]


@pytest.mark.asyncio
async def test_run_next_runs_the_first_ticket_in_patrol_order():
    first = _task()
    second = _task()
    second.id = "2"
    manager = _TicketManager([first, second])
    seen: list[WorkflowInvocation] = []

    async def run_workflow(invocation: WorkflowInvocation) -> str:
        seen.append(invocation)
        return "done"

    selector = TicketSelector(_Context(manager), source="manual")  # type: ignore[arg-type]
    result = await selector.run_next(_Person(), run_workflow)  # type: ignore[arg-type]

    assert result == "done"
    [invocation] = seen
    assert invocation.source == "manual"
    assert invocation.payload["task"]["id"] == "1"
    assert manager.moved == [("1", Task.IN_PROGRESS)]


@pytest.mark.asyncio
async def test_run_next_without_work_runs_nothing():
    async def run_workflow(invocation: WorkflowInvocation) -> None:
        raise AssertionError("no ticket, no workflow")

    selector = TicketSelector(_Context(_TicketManager()))  # type: ignore[arg-type]

    assert await selector.run_next(_Person(), run_workflow) is None  # type: ignore[arg-type]


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
        pull_request_url="https://github.com/owner/repo/pull/7",
    )
    assert pr.trace_attributes() == {
        "github.repo": "repo",
        "github.title": "T",
        "github.kind": "pull_request",
        "github.url": "https://github.com/owner/repo/pull/7",
        "github.number": "7",
    }
