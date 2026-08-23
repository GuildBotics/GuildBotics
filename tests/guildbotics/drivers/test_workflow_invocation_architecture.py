from __future__ import annotations

import asyncio
import datetime
import types

import pytest

from guildbotics.drivers.task_scheduler import TaskScheduler
from guildbotics.drivers.ticket_selector import TicketSelector
from guildbotics.drivers.workflow_dispatcher import WorkflowDispatcher
from guildbotics.entities.task import Task
from guildbotics.entities.team import Person
from guildbotics.runtime.workflow_invocation import (
    WORKFLOW_INVOCATION_KEY,
    WorkflowInvocation,
)


class _FakeTicketManager:
    def __init__(self, task: Task | None = None) -> None:
        self._task = task
        self.comments: list[tuple[Task, str]] = []

    async def get_task_to_work_on(self) -> Task | None:
        return self._task

    async def get_ticket_url(self, task: Task, markdown: bool = False) -> str:
        return "https://github.com/fake/repo/issues/123"

    async def add_comment_to_ticket(self, task: Task, message: str) -> None:
        self.comments.append((task, message))


class _FakeContext:
    def __init__(self, ticket_manager: _FakeTicketManager | None = None) -> None:
        self.person = Person(person_id="alice", name="A", is_active=True)
        self.team = types.SimpleNamespace(
            project=types.SimpleNamespace(
                get_language_code=lambda: "en",
                get_language_name=lambda: "English",
            ),
            members=[self.person],
        )
        self.logger = types.SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            error=lambda *a, **k: None,
        )
        self.task = Task(title="T", description="D")
        self.pipe = ""
        self.shared_state: dict = {}
        self._ticket_manager = ticket_manager or _FakeTicketManager()
        self.clones: list = []

    def clone_for(self, person: Person) -> _FakeContext:
        clone = _FakeContext(ticket_manager=self._ticket_manager)
        clone.person = person
        clone.shared_state = {}

        async def _aclose():
            clone.closed = True

        clone.aclose = _aclose  # type: ignore[attr-defined]
        self.clones.append(clone)
        return clone

    def get_ticket_manager(self) -> _FakeTicketManager:
        return self._ticket_manager


def test_workflow_invocation_dataclass():
    inv = WorkflowInvocation(
        command="test_command",
        person_id="alice",
        source="routine",
        trigger_type="ticket",
        payload={"foo": "bar"},
        idempotency_key="key",
    )
    assert inv.command == "test_command"
    assert inv.person_id == "alice"
    assert inv.source == "routine"
    assert inv.trigger_type == "ticket"
    assert inv.payload == {"foo": "bar"}
    assert inv.idempotency_key == "key"


@pytest.mark.asyncio
async def test_workflow_dispatcher_dispatch(monkeypatch):
    ran = []

    class _FakeRunner:
        def __init__(self, context, command, args):
            ran.append((context, command, args))

        async def run(self):
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _FakeRunner
    )

    context = _FakeContext()
    dispatcher = WorkflowDispatcher(context, service_run_id="run-123")  # type: ignore[arg-type]

    inv = WorkflowInvocation(
        command="workflows/chat_conversation_workflow",
        person_id="alice",
        source="event_queue",
        trigger_type="chat",
        payload={
            "service_name": "slack",
            "channel_id": "C123",
            "event": {"event_id": "E123"},
        },
    )

    await dispatcher.dispatch(inv, context.person)

    assert len(ran) == 1
    ctx_used, command, args = ran[0]
    assert command == "workflows/chat_conversation_workflow"
    assert args == []

    # Check context shared state injections
    assert ctx_used.shared_state[WORKFLOW_INVOCATION_KEY] == inv
    assert getattr(ctx_used, "closed", False) is True


@pytest.mark.asyncio
async def test_ticket_selector_returns_invocation():
    task = Task(
        title="Fix bug",
        description="Fix it",
        pull_request_url="https://github.com/pr",
        trigger_reason="assigned",
    )
    ticket_mgr = _FakeTicketManager(task)
    context = _FakeContext(ticket_manager=ticket_mgr)

    selector = TicketSelector(context)  # type: ignore[arg-type]
    inv = await selector.select(context.person)

    assert inv is not None
    assert inv.command == "workflows/ticket_driven_workflow"
    assert inv.person_id == "alice"
    assert inv.source == "routine"
    assert inv.trigger_type == "ticket"
    assert inv.payload["task"]["title"] == "Fix bug"
    assert inv.payload["ticket_url"] == "https://github.com/fake/repo/issues/123"
    assert inv.payload["pull_request_url"] == "https://github.com/pr"
    assert inv.payload["trigger_reason"] == "assigned"
    assert (
        "github:ticket:alice:https://github.com/fake/repo/issues/123"
        in inv.idempotency_key
    )


@pytest.mark.asyncio
async def test_ticket_selector_returns_none_if_no_task():
    ticket_mgr = _FakeTicketManager(None)
    context = _FakeContext(ticket_manager=ticket_mgr)

    selector = TicketSelector(context)  # type: ignore[arg-type]
    inv = await selector.select(context.person)
    assert inv is None


def test_task_scheduler_uses_ticket_selector(monkeypatch):
    class _FakeTicketSelector:
        def __init__(self, context):
            pass

        async def select(self, person):
            return WorkflowInvocation(
                command="workflows/ticket_driven_workflow",
                person_id="alice",
                source="routine",
                trigger_type="ticket",
                payload={"task": {"title": "stub", "description": "stub"}},
            )

    dispatched = []

    class _FakeDispatcher:
        def __init__(self, context, service_run_id=None):
            pass

        async def dispatch(self, invocation, person):
            dispatched.append((invocation, person))

    monkeypatch.setattr(
        "guildbotics.drivers.ticket_selector.TicketSelector", _FakeTicketSelector
    )
    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.WorkflowDispatcher", _FakeDispatcher
    )

    context = _FakeContext()
    scheduler = TaskScheduler(
        context=context,  # type: ignore[arg-type]
        routine_interval_minutes=10,
    )
    loop = asyncio.new_event_loop()
    try:
        routine_commands = ["workflows/ticket_driven_workflow"]
        routine_command_index = 0
        next_routine_time = None
        start_time = datetime.datetime.now()
        consecutive_errors = 0

        routine_command_index, consecutive_errors, next_routine_time, should_stop = (
            scheduler._process_routine_tasks(
                loop,
                context,  # type: ignore[arg-type]
                context.person,
                routine_commands,
                routine_command_index,
                next_routine_time,
                start_time,
                consecutive_errors,
            )
        )

        assert routine_command_index == 1
        assert consecutive_errors == 0
        assert next_routine_time is not None
        assert should_stop is False
        assert len(dispatched) == 1
        assert dispatched[0][0].command == "workflows/ticket_driven_workflow"
    finally:
        loop.close()


def test_task_scheduler_ticket_selector_returns_none(monkeypatch):
    class _FakeTicketSelector:
        def __init__(self, context):
            pass

        async def select(self, person):
            return None

    dispatched = []

    class _FakeDispatcher:
        def __init__(self, context, service_run_id=None):
            pass

        async def dispatch(self, invocation, person):
            dispatched.append((invocation, person))

    monkeypatch.setattr(
        "guildbotics.drivers.ticket_selector.TicketSelector", _FakeTicketSelector
    )
    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.WorkflowDispatcher", _FakeDispatcher
    )

    context = _FakeContext()
    scheduler = TaskScheduler(
        context=context,  # type: ignore[arg-type]
        routine_interval_minutes=10,
    )
    loop = asyncio.new_event_loop()
    try:
        routine_commands = ["workflows/ticket_driven_workflow"]
        routine_command_index = 0
        next_routine_time = None
        start_time = datetime.datetime.now()
        consecutive_errors = 3

        routine_command_index, consecutive_errors, next_routine_time, should_stop = (
            scheduler._process_routine_tasks(
                loop,
                context,  # type: ignore[arg-type]
                context.person,
                routine_commands,
                routine_command_index,
                next_routine_time,
                start_time,
                consecutive_errors,
            )
        )

        assert routine_command_index == 1
        assert consecutive_errors == 0
        assert next_routine_time is not None
        assert should_stop is False
        assert len(dispatched) == 0
        assert len(context.get_ticket_manager().comments) == 0
    finally:
        loop.close()


def test_task_scheduler_ticket_selector_raises_error(monkeypatch):
    worker_events = []

    class _FakeTicketSelector:
        def __init__(self, context):
            pass

        async def select(self, person):
            raise RuntimeError("Selector API Failure")

    dispatched = []

    class _FakeDispatcher:
        def __init__(self, context, service_run_id=None):
            pass

        async def dispatch(self, invocation, person):
            dispatched.append((invocation, person))

    monkeypatch.setattr(
        "guildbotics.drivers.ticket_selector.TicketSelector", _FakeTicketSelector
    )
    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.WorkflowDispatcher", _FakeDispatcher
    )
    monkeypatch.setattr(
        "guildbotics.drivers.task_scheduler.record_correlated_event",
        lambda **kwargs: worker_events.append(kwargs),
    )

    context = _FakeContext()
    scheduler = TaskScheduler(
        context=context,  # type: ignore[arg-type]
        routine_interval_minutes=10,
        consecutive_error_limit=3,
    )
    loop = asyncio.new_event_loop()
    try:
        routine_commands = ["workflows/ticket_driven_workflow"]
        routine_command_index = 0
        next_routine_time = None
        start_time = datetime.datetime.now()

        consecutive_errors = 1
        routine_command_index, consecutive_errors, next_routine_time, should_stop = (
            scheduler._process_routine_tasks(
                loop,
                context,  # type: ignore[arg-type]
                context.person,
                routine_commands,
                routine_command_index,
                next_routine_time,
                start_time,
                consecutive_errors,
            )
        )
        assert consecutive_errors == 2  # noqa: PLR2004
        assert should_stop is False
        assert len(dispatched) == 0
        assert len(context.get_ticket_manager().comments) == 0

        routine_command_index, consecutive_errors, next_routine_time, should_stop = (
            scheduler._process_routine_tasks(
                loop,
                context,  # type: ignore[arg-type]
                context.person,
                routine_commands,
                routine_command_index,
                None,
                start_time,
                consecutive_errors,
            )
        )
        assert consecutive_errors == 3  # noqa: PLR2004
        assert should_stop is True
        assert len(dispatched) == 0
        assert len(context.get_ticket_manager().comments) == 0
        assert worker_events == [
            {
                "event_type": "scheduler.worker.failed",
                "default_source": "scheduler",
                "person_id": context.person.person_id,
                "attributes": {"service_run_id": ""},
                "payload": {
                    "source": "routine",
                    "consecutive_errors": 3,
                    "consecutive_error_limit": 3,
                },
            }
        ]
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_run_command_malformed():
    from guildbotics.drivers.utils import run_command

    context = _FakeContext()
    ok = await run_command(context, 'echo "unterminated', "scheduled")
    assert ok is False


def test_task_scheduler_malformed_scheduled_command():
    from guildbotics.drivers.task_scheduler import ScheduledCommand

    context = _FakeContext()
    scheduler = TaskScheduler(
        context=context,  # type: ignore[arg-type]
        routine_interval_minutes=10,
    )
    cmd = ScheduledCommand(command='echo "unterminated', schedule="* * * * *")
    loop = asyncio.new_event_loop()
    try:
        consecutive_errors, should_stop = scheduler._process_scheduled_tasks(
            loop,
            context,  # type: ignore[arg-type]
            context.person,
            [cmd],
            datetime.datetime.now() + datetime.timedelta(minutes=2),
            0,
        )
        assert consecutive_errors == 1
        assert should_stop is False
    finally:
        loop.close()


def test_task_scheduler_routine_with_args_falls_back_to_run_command(monkeypatch):
    run_command_called = []

    async def fake_run_command(context, command, task_type):
        run_command_called.append((command, task_type))
        return True

    monkeypatch.setattr(
        "guildbotics.drivers.task_scheduler.run_command", fake_run_command
    )

    context = _FakeContext()
    scheduler = TaskScheduler(
        context=context,  # type: ignore[arg-type]
        routine_interval_minutes=10,
    )
    loop = asyncio.new_event_loop()
    try:
        routine_commands = ["workflows/ticket_driven_workflow --foo"]
        _idx, _err, _next_time, _stop = scheduler._process_routine_tasks(
            loop,
            context,  # type: ignore[arg-type]
            context.person,
            routine_commands,
            0,
            None,
            datetime.datetime.now(),
            0,
        )
        assert len(run_command_called) == 1
        assert run_command_called[0][0] == "workflows/ticket_driven_workflow --foo"
        assert run_command_called[0][1] == "routine"
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_dispatcher_reuses_active_trace(monkeypatch):
    from guildbotics.observability import current_trace, trace_scope

    seen = {}

    class _FakeRunner:
        def __init__(self, context, command, args):
            pass

        async def run(self):
            t = current_trace()
            seen["trace_id"] = t.trace_id if t else None
            seen["attributes"] = dict(t.attributes) if t else {}

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _FakeRunner
    )
    context = _FakeContext()
    dispatcher = WorkflowDispatcher(context, service_run_id="run-123")
    inv = WorkflowInvocation(
        command="workflows/ticket_driven_workflow",
        person_id="alice",
        source="routine",
        trigger_type="ticket",
        payload={"task": {"title": "t", "description": "d"}},
    )
    with trace_scope("routine", person_id="alice") as outer:
        await dispatcher.dispatch(inv, context.person)

    assert seen["trace_id"] == outer.trace_id
    assert seen["attributes"].get("service_run_id") == "run-123"


@pytest.mark.asyncio
async def test_dispatcher_creates_new_trace_when_none_active(monkeypatch):
    from guildbotics.observability import current_trace

    seen = {}

    class _FakeRunner:
        def __init__(self, context, command, args):
            pass

        async def run(self):
            t = current_trace()
            seen["trace_id"] = t.trace_id if t else None
            seen["source"] = t.source if t else None
            seen["attributes"] = dict(t.attributes) if t else {}

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _FakeRunner
    )
    context = _FakeContext()
    dispatcher = WorkflowDispatcher(context, service_run_id="run-123")
    inv = WorkflowInvocation(
        command="workflows/ticket_driven_workflow",
        person_id="alice",
        source="routine",
        trigger_type="ticket",
        payload={"task": {"title": "t", "description": "d"}},
    )
    await dispatcher.dispatch(inv, context.person)

    assert seen["trace_id"] is not None
    assert seen["source"] == "routine"
    assert seen["attributes"].get("service_run_id") == "run-123"


@pytest.mark.asyncio
async def test_run_command_empty():
    from guildbotics.drivers.utils import run_command

    context = _FakeContext()
    ok = await run_command(context, "   ", "scheduled")
    assert ok is False
