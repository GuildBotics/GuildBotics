import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.drivers import task_scheduler
from guildbotics.drivers.task_scheduler import TaskScheduler
from guildbotics.entities.task import Task
from guildbotics.observability import current_trace
from guildbotics.runtime.workflow_invocation import WorkflowInvocation
from guildbotics.utils.i18n_tool import t

EXPECTED_ROUTINE_CALL_COUNT = 2


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    # Keep the per-member chat dispatcher pointed at an empty temp workspace so it
    # is a no-op (no queued chat events) in these scheduler timing tests.
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))


@pytest.fixture(autouse=True)
def _environment_ready(monkeypatch):
    """The device can run AI CLI turns unless a test says otherwise."""
    _set_environment_refusal(monkeypatch, "")


def _set_environment_refusal(monkeypatch, refusal: str) -> None:
    monkeypatch.setattr(
        task_scheduler, "device_status", lambda: SimpleNamespace(refusal=refusal)
    )


class _Logger:
    def info(self, message: str, *args: object) -> None:
        return None

    def debug(self, message: str) -> None:
        return None

    def warning(self, message: str) -> None:
        return None

    def error(self, message: str) -> None:
        return None


class _Context:
    def __init__(self, member: object) -> None:
        self.team = SimpleNamespace(members=[member])
        self.person = member
        self.logger = _Logger()

    def clone_for(self, person: object) -> "_Context":
        return self

    async def aclose(self) -> None:
        return None


class _Person:
    def __init__(self, routine_commands: list[str] | None = None) -> None:
        self.person_id = "alice"
        self.is_active = True
        self.routine_commands: list[str] = routine_commands or []

    def get_scheduled_commands(self) -> list[object]:
        return []


def test_task_scheduler_runs_routine_at_configured_minute_interval(monkeypatch) -> None:
    class FakeDateTime(dt.datetime):
        current = dt.datetime(2026, 1, 1, 9, 0, 0)

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            if tz is None:
                return cls.current
            return cls.current.replace(tzinfo=tz)

    person = _Person(["routine"])
    scheduler = TaskScheduler(
        _Context(person),
        routine_interval_minutes=3,
    )
    calls: list[dt.datetime] = []

    async def fake_run_command(
        context: object,
        command: str,
        task_type: str,
    ) -> bool:
        calls.append(FakeDateTime.current)
        if len(calls) == EXPECTED_ROUTINE_CALL_COUNT:
            scheduler.shutdown()
        return True

    def fake_sleep(seconds: float) -> None:
        FakeDateTime.current += dt.timedelta(seconds=seconds)

    monkeypatch.setattr(task_scheduler.datetime, "datetime", FakeDateTime)
    monkeypatch.setattr(task_scheduler, "run_command", fake_run_command)
    monkeypatch.setattr(scheduler, "_sleep_interruptible", fake_sleep)

    scheduler._process_tasks_list(person, [])

    assert calls == [
        dt.datetime(2026, 1, 1, 9, 0, 0),
        dt.datetime(2026, 1, 1, 9, 3, 0),
    ]


def test_task_scheduler_measures_routine_interval_after_routine_finishes(
    monkeypatch,
) -> None:
    class FakeDateTime(dt.datetime):
        current = dt.datetime(2026, 1, 1, 9, 0, 0)

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            if tz is None:
                return cls.current
            return cls.current.replace(tzinfo=tz)

    person = _Person(["routine"])
    scheduler = TaskScheduler(
        _Context(person),
        routine_interval_minutes=3,
    )
    calls: list[dt.datetime] = []
    first_finished_at: dt.datetime | None = None

    async def fake_run_command(
        context: object,
        command: str,
        task_type: str,
    ) -> bool:
        nonlocal first_finished_at
        calls.append(FakeDateTime.current)
        if len(calls) == 1:
            FakeDateTime.current += dt.timedelta(minutes=2)
            first_finished_at = FakeDateTime.current
        if len(calls) == EXPECTED_ROUTINE_CALL_COUNT:
            scheduler.shutdown()
        return True

    def fake_sleep(seconds: float) -> None:
        FakeDateTime.current += dt.timedelta(seconds=seconds)

    monkeypatch.setattr(task_scheduler.datetime, "datetime", FakeDateTime)
    monkeypatch.setattr(task_scheduler, "run_command", fake_run_command)
    monkeypatch.setattr(scheduler, "_sleep_interruptible", fake_sleep)

    scheduler._process_tasks_list(person, [])

    assert first_finished_at is not None
    assert calls[1] >= first_finished_at + dt.timedelta(minutes=3)


def test_routine_work_is_tracked_under_its_trace_id(monkeypatch) -> None:
    person = _Person(["routine"])
    scheduler = TaskScheduler(_Context(person), routine_interval_minutes=3)
    seen: list[tuple[str | None, list[str]]] = []

    async def fake_run_command(context, command, task_type) -> bool:
        trace = current_trace()
        works = scheduler._execution.snapshot()
        seen.append((trace.trace_id if trace else None, [work.id for work in works]))
        scheduler.shutdown()
        return True

    monkeypatch.setattr(task_scheduler, "run_command", fake_run_command)
    monkeypatch.setattr(scheduler, "_sleep_interruptible", lambda seconds: None)

    scheduler._process_tasks_list(person, [])

    trace_id, work_ids = seen[0]
    assert trace_id is not None
    assert work_ids == [trace_id]


def test_routine_ticket_patrol_selects_outside_any_trace_or_tracked_work(
    monkeypatch,
) -> None:
    person = _Person(["workflows/ticket_driven_workflow"])
    scheduler = TaskScheduler(_Context(person), routine_interval_minutes=3)
    seen: list[tuple[str | None, list[str]]] = []

    def fake_patrol(loop, context, person, command, start_time) -> tuple[bool, bool]:
        trace = current_trace()
        works = scheduler._execution.snapshot()
        seen.append((trace.trace_id if trace else None, [work.id for work in works]))
        scheduler.shutdown()
        return True, True

    monkeypatch.setattr(scheduler, "_patrol_tickets", fake_patrol)
    monkeypatch.setattr(scheduler, "_sleep_interruptible", lambda seconds: None)

    scheduler._process_tasks_list(person, [])

    # The caller must not pre-open a trace nor claim work: an idle patrol
    # leaves neither diagnostics records nor a task-run record, so the patrol
    # opens both only for a ticket it actually dispatches.
    assert seen == [(None, [])]


@pytest.mark.parametrize("setting", ["building", "filesystem"])
def test_ticket_patrol_is_deferred_while_the_environment_is_unavailable(
    monkeypatch,
    setting,
) -> None:
    person = _Person(["workflows/ticket_driven_workflow"])
    warnings: list[str] = []
    context = _Context(person)
    context.logger.warning = warnings.append  # type: ignore[method-assign]
    scheduler = TaskScheduler(
        context, routine_interval_minutes=3, consecutive_error_limit=1
    )
    dispatched: list[str] = []

    def fake_patrol(loop, context, person, command, start_time) -> bool:
        dispatched.append(command)
        return True

    monkeypatch.setattr(scheduler, "_patrol_tickets", fake_patrol)
    reason = (
        t("intelligences.agent_environment.filesystem.macos_documents", app="")
        if setting == "filesystem"
        else t("intelligences.agent_environment.snapshot.building")
    )
    _set_environment_refusal(monkeypatch, reason)

    index, errors, next_at, should_stop = scheduler._process_routine_tasks(
        None, context, person, person.routine_commands, 0, None, dt.datetime.now(), 0
    )
    scheduler._process_routine_tasks(
        None,
        context,
        person,
        person.routine_commands,
        index,
        None,
        dt.datetime.now(),
        0,
    )

    # Nothing was dispatched, nothing failed: the worker stays up (the limit
    # is 1) and the patrol is simply due again later.
    assert dispatched == []
    assert (errors, should_stop) == (0, False)
    assert next_at is not None
    # Said once, not once per patrol.
    assert warnings == [f"AI CLI work is deferred on this device: {reason}"]


@pytest.mark.asyncio
async def test_pending_chat_is_deferred_while_the_environment_is_unavailable(
    monkeypatch,
) -> None:
    scheduler = TaskScheduler(_Context(_Person()))
    calls: list[str] = []

    async def _fake_process(person, stop_event=None):
        calls.append(person.person_id)
        return 1

    scheduler._chat_dispatcher.process_person = _fake_process  # type: ignore[assignment]
    _set_environment_refusal(monkeypatch, "no runtime")

    assert await scheduler._process_pending_chat(_Person()) is True
    assert calls == []


def _patrol(scheduler: TaskScheduler) -> tuple[bool, bool]:
    loop = asyncio.new_event_loop()
    try:
        return scheduler._patrol_tickets(
            loop,
            _Context(_Person()),
            _Person(),
            "workflows/ticket_driven_workflow",
            dt.datetime(2026, 1, 1, 9, 0, 0),
        )
    finally:
        loop.close()


def _ticket_task(number: int = 7) -> Task:
    return Task(
        id=str(number),
        title="ログイン修正",
        description="",
        repository="o/r",
        number=number,
        url=f"https://github.com/o/r/issues/{number}",
    )


def _ticket_invocation(task: Task | None = None) -> WorkflowInvocation:
    task = task or _ticket_task()
    return WorkflowInvocation(
        command="workflows/ticket_driven_workflow",
        person_id="alice",
        source="routine",
        trigger_type="ticket",
        payload={
            "task": task.model_dump(),
            "ticket_url": task.url,
            "trigger_reason": task.trigger_reason or "",
        },
    )


async def _passthrough_run(self, person, invocation, run_workflow):
    """The selector's settlement is covered by ``test_ticket_selector.py``."""
    return await run_workflow(invocation)


def test_ticket_patrol_idle_leaves_no_trace_and_no_run_record(monkeypatch) -> None:
    from guildbotics.drivers import ticket_selector
    from guildbotics.drivers import utils as driver_utils

    scheduler = TaskScheduler(_Context(_Person()))

    async def fake_candidates(self, person):
        return []

    async def forbidden_run_with_logging(*args, **kwargs):
        raise AssertionError("an idle patrol must not create trace records")

    monkeypatch.setattr(ticket_selector.TicketSelector, "candidates", fake_candidates)
    monkeypatch.setattr(driver_utils, "run_with_logging", forbidden_run_with_logging)

    assert _patrol(scheduler) == (True, True)
    assert list(RunStore().records()) == []


def test_ticket_patrol_dispatches_as_tracked_work_under_a_titled_trace(
    monkeypatch,
) -> None:
    from guildbotics.drivers import ticket_selector, workflow_dispatcher
    from guildbotics.drivers import utils as driver_utils

    scheduler = TaskScheduler(_Context(_Person()))
    task = _ticket_task()
    invocation = _ticket_invocation()
    dispatched: list[tuple[str | None, dict[str, object], list[str], object]] = []

    async def fake_candidates(self, person):
        return [task]

    async def fake_refresh(self, person, candidate):
        assert candidate is task
        return invocation

    class FakeDispatcher:
        def __init__(self, context, service_run_id=None):
            pass

        async def dispatch(self, inv, person):
            trace = current_trace()
            works = scheduler._execution.snapshot()
            dispatched.append(
                (
                    trace.trace_id if trace else None,
                    dict(trace.attributes) if trace else {},
                    [work.id for work in works],
                    inv,
                )
            )

    async def fake_run_with_logging(context, command, task_type, action):
        await action()
        return True

    monkeypatch.setattr(ticket_selector.TicketSelector, "candidates", fake_candidates)
    monkeypatch.setattr(ticket_selector.TicketSelector, "refresh", fake_refresh)
    monkeypatch.setattr(ticket_selector.TicketSelector, "run", _passthrough_run)
    monkeypatch.setattr(workflow_dispatcher, "WorkflowDispatcher", FakeDispatcher)
    monkeypatch.setattr(driver_utils, "run_with_logging", fake_run_with_logging)

    assert _patrol(scheduler) == (True, True)
    trace_id, attributes, work_ids, inv = dispatched[0]
    assert inv is invocation
    assert trace_id is not None
    # The dispatch is tracked work under its own trace, and the trace opens
    # with the ticket's attributes so the run record names the issue.
    assert work_ids == [trace_id]
    assert attributes["github.title"] == "ログイン修正"
    assert attributes["github.url"] == "https://github.com/o/r/issues/7"
    records = list(RunStore().records())
    assert [record.run_id for record in records] == [trace_id]
    assert records[0].source == "routine"
    assert records[0].attributes["github.title"] == "ログイン修正"
    assert records[0].work_identity == {
        "kind": "routine",
        "person_id": "alice",
        "command": "workflows/ticket_driven_workflow",
        "slot": "2026-01-01T09:00:00",
        "ticket_url": "https://github.com/o/r/issues/7",
        "trigger_reason": "",
    }
    assert records[0].status == "succeeded"


def test_failed_ticket_patrol_is_recorded_as_failed_and_counted(monkeypatch) -> None:
    from guildbotics.drivers import ticket_selector, workflow_dispatcher

    scheduler = TaskScheduler(_Context(_Person()))

    async def fake_candidates(self, person):
        return [_ticket_task()]

    async def fake_refresh(self, person, candidate):
        return _ticket_invocation(candidate)

    class FailingDispatcher:
        def __init__(self, context, service_run_id=None):
            pass

        async def dispatch(self, invocation, person):
            raise RuntimeError("the turn never recorded a completion")

    monkeypatch.setattr(ticket_selector.TicketSelector, "candidates", fake_candidates)
    monkeypatch.setattr(ticket_selector.TicketSelector, "refresh", fake_refresh)
    monkeypatch.setattr(ticket_selector.TicketSelector, "run", _passthrough_run)
    monkeypatch.setattr(workflow_dispatcher, "WorkflowDispatcher", FailingDispatcher)

    assert _patrol(scheduler) == (False, True)
    assert [record.status for record in RunStore().records()] == ["failed"]


def test_ticket_patrol_keeps_next_candidate_due_and_uses_distinct_identity(
    monkeypatch,
) -> None:
    from guildbotics.drivers import ticket_selector, workflow_dispatcher
    from guildbotics.drivers import utils as driver_utils

    person = _Person(["workflows/ticket_driven_workflow"])
    context = _Context(person)
    scheduler = TaskScheduler(context, routine_interval_minutes=10)
    first = _ticket_task()
    second = _ticket_task(8)
    second.trigger_reason = "issue_comment"
    dispatched: list[WorkflowInvocation] = []

    async def fake_candidates(self, selected_person):
        assert selected_person is person
        return [first, second]

    async def fake_refresh(self, selected_person, candidate):
        return _ticket_invocation(candidate)

    class FakeDispatcher:
        def __init__(self, selected_context, service_run_id=None):
            pass

        async def dispatch(self, invocation, selected_person):
            dispatched.append(invocation)

    async def fake_run_with_logging(context, command, task_type, action):
        await action()
        return True

    monkeypatch.setattr(ticket_selector.TicketSelector, "candidates", fake_candidates)
    monkeypatch.setattr(ticket_selector.TicketSelector, "refresh", fake_refresh)
    monkeypatch.setattr(ticket_selector.TicketSelector, "run", _passthrough_run)
    monkeypatch.setattr(workflow_dispatcher, "WorkflowDispatcher", FakeDispatcher)
    monkeypatch.setattr(driver_utils, "run_with_logging", fake_run_with_logging)

    loop = asyncio.new_event_loop()
    try:
        index, errors, next_at, stopped = scheduler._process_routine_tasks(
            loop,
            context,
            person,
            person.routine_commands,
            0,
            None,
            dt.datetime(2026, 1, 1, 9, 0, 0),
            0,
        )
        assert (index, errors, next_at, stopped) == (1, 0, None, False)

        index, errors, next_at, stopped = scheduler._process_routine_tasks(
            loop,
            context,
            person,
            person.routine_commands,
            index,
            next_at,
            dt.datetime(2026, 1, 1, 9, 1, 0),
            errors,
        )
    finally:
        loop.close()

    assert [invocation.payload["ticket_url"] for invocation in dispatched] == [
        "https://github.com/o/r/issues/7",
        "https://github.com/o/r/issues/8",
    ]
    assert index == 1
    assert errors == 0
    assert next_at is not None
    assert stopped is False
    records = list(RunStore().records())
    assert {record.work_identity["ticket_url"] for record in records} == {
        "https://github.com/o/r/issues/7",
        "https://github.com/o/r/issues/8",
    }


def test_ticket_patrol_selection_failure_is_recorded_under_a_trace(
    monkeypatch,
) -> None:
    from guildbotics.drivers import ticket_selector
    from guildbotics.drivers import utils as driver_utils

    scheduler = TaskScheduler(_Context(_Person()))
    failure = RuntimeError("boom")
    recorded: list[tuple[str | None, BaseException | None]] = []

    async def fake_candidates(self, person):
        raise failure

    async def fake_run_with_logging(context, command, task_type, action):
        trace = current_trace()
        try:
            await action()
        except Exception as exc:
            # Like the real one: record the failure, then re-raise it.
            recorded.append((trace.trace_id if trace else None, exc))
            raise
        recorded.append((trace.trace_id if trace else None, None))
        return True

    monkeypatch.setattr(ticket_selector.TicketSelector, "candidates", fake_candidates)
    monkeypatch.setattr(driver_utils, "run_with_logging", fake_run_with_logging)

    assert _patrol(scheduler) == (False, True)
    assert len(recorded) == 1
    assert recorded[0][0] is not None
    assert recorded[0][1] is failure
    # A selection that failed took no work, so it leaves no run record.
    assert list(RunStore().records()) == []


def test_routine_run_updates_member_routine_heartbeat(monkeypatch) -> None:
    class FakeDateTime(dt.datetime):
        current = dt.datetime(2026, 1, 1, 9, 0, 0)

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            if tz is None:
                return cls.current
            return cls.current.replace(tzinfo=tz)

    person = _Person(["routine"])
    scheduler = TaskScheduler(_Context(person), routine_interval_minutes=3)

    async def fake_run_command(context, command, task_type) -> bool:
        scheduler.shutdown()
        return True

    def fake_sleep(seconds: float) -> None:
        FakeDateTime.current += dt.timedelta(seconds=seconds)

    monkeypatch.setattr(task_scheduler.datetime, "datetime", FakeDateTime)
    monkeypatch.setattr(task_scheduler, "run_command", fake_run_command)
    monkeypatch.setattr(scheduler, "_sleep_interruptible", fake_sleep)

    assert scheduler.get_status_summary()["member_routines"] == []

    scheduler._process_tasks_list(person, [])

    routines = scheduler.get_status_summary()["member_routines"]
    assert [entry["person_id"] for entry in routines] == ["alice"]
    last = dt.datetime.fromisoformat(routines[0]["last_routine_at"])
    next_at = dt.datetime.fromisoformat(routines[0]["next_routine_at"])
    assert next_at - last == dt.timedelta(minutes=3)


def test_scheduled_work_is_tracked_under_its_trace_id(monkeypatch) -> None:
    person = _Person()
    scheduler = TaskScheduler(_Context(person))
    seen: list[tuple[str | None, list[str]]] = []

    async def fake_run_command(context, command, task_type) -> bool:
        trace = current_trace()
        works = scheduler._execution.snapshot()
        seen.append((trace.trace_id if trace else None, [work.id for work in works]))
        scheduler.shutdown()
        return True

    monkeypatch.setattr(task_scheduler, "run_command", fake_run_command)
    monkeypatch.setattr(scheduler, "_sleep_interruptible", lambda seconds: None)
    scheduled = SimpleNamespace(command="sched", should_run=lambda now: True)

    scheduler._process_tasks_list(person, [scheduled])

    trace_id, work_ids = seen[0]
    assert trace_id is not None
    assert work_ids == [trace_id]


class _FailingRunner:
    """A command that raises, standing in for any failed scheduler command."""

    def __init__(self, context, name, args, cwd=None) -> None:
        self.command_name = name

    async def run(self) -> str:
        raise RuntimeError("boom")


def _run_scheduler_slot(scheduler: TaskScheduler, source: str) -> tuple[int, bool]:
    person = scheduler.context.team.members[0]
    loop = asyncio.new_event_loop()
    start = dt.datetime(2026, 1, 1, 9, 0, 0)
    try:
        if source == "scheduled":
            scheduled = SimpleNamespace(command="sched", should_run=lambda now: True)
            return scheduler._process_scheduled_tasks(
                loop, scheduler.context, person, [scheduled], start, 0
            )
        _, errors, _, stopped = scheduler._process_routine_tasks(
            loop, scheduler.context, person, ["routine"], 0, None, start, 0
        )
        return errors, stopped
    finally:
        loop.close()


@pytest.mark.parametrize("source", ["scheduled", "routine"])
def test_failed_scheduler_command_is_recorded_as_failed(monkeypatch, source) -> None:
    from guildbotics.drivers import utils

    scheduler = TaskScheduler(_Context(_Person()))
    monkeypatch.setattr(utils, "CommandRunner", _FailingRunner)
    monkeypatch.setattr(scheduler, "_sleep_interruptible", lambda seconds: None)

    assert _run_scheduler_slot(scheduler, source) == (1, False)

    records = list(RunStore().records())
    assert [(record.source, record.status) for record in records] == [
        (source, "failed")
    ]


def test_force_stopped_scheduler_command_is_recorded_as_cancelled(
    monkeypatch,
) -> None:
    from guildbotics.drivers import utils

    scheduler = TaskScheduler(_Context(_Person()))

    class _StoppedRunner(_FailingRunner):
        async def run(self) -> str:
            scheduler.request_shutdown(graceful=False)
            await asyncio.sleep(30)
            return ""

    monkeypatch.setattr(utils, "CommandRunner", _StoppedRunner)
    monkeypatch.setattr(scheduler, "_sleep_interruptible", lambda seconds: None)

    # A stop is not a command error, so the worker does not count it.
    assert _run_scheduler_slot(scheduler, "routine") == (0, False)

    assert [record.status for record in RunStore().records()] == ["cancelled"]


@pytest.mark.asyncio
async def test_process_pending_chat_delegates_to_dispatcher() -> None:
    scheduler = TaskScheduler(_Context(_Person()))
    calls: list[str] = []

    async def _fake_process(person, stop_event=None):
        calls.append(person.person_id)
        return 1

    scheduler._chat_dispatcher.process_person = _fake_process  # type: ignore[assignment]

    ok = await scheduler._process_pending_chat(_Person())

    assert ok is True
    assert calls == ["alice"]


@pytest.mark.asyncio
async def test_run_cancellable_allows_long_task_to_finish_on_graceful_stop() -> None:
    import asyncio

    scheduler = TaskScheduler(_Context(_Person()))
    cancelled = {"value": False}
    started = asyncio.Event()
    finish = asyncio.Event()

    async def _long():
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            cancelled["value"] = True
            raise
        return True

    task = asyncio.ensure_future(scheduler._run_cancellable(_long()))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    scheduler._stop_event.set()
    await asyncio.sleep(0)
    assert task.done() is False

    finish.set()
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result is True
    assert cancelled["value"] is False


@pytest.mark.asyncio
async def test_run_cancellable_cancels_long_task_on_force_stop() -> None:
    import asyncio

    scheduler = TaskScheduler(_Context(_Person()))
    cancelled = {"value": False}
    started = asyncio.Event()

    async def _long():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled["value"] = True
            raise
        return True

    task = asyncio.ensure_future(scheduler._run_cancellable(_long()))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    scheduler._cancel_event.set()

    result = await asyncio.wait_for(task, timeout=1.0)
    assert result is False
    assert cancelled["value"] is True


def test_update_consecutive_errors_ignores_failures_during_shutdown() -> None:
    scheduler = TaskScheduler(_Context(_Person()))
    scheduler._stop_event.set()

    count, should_stop = scheduler._update_consecutive_errors(
        False, source="scheduled", consecutive_errors=2
    )

    assert (count, should_stop) == (2, False)


def test_run_work_rejection_during_drain_mirrors_stop() -> None:
    import asyncio

    scheduler = TaskScheduler(_Context(_Person()))
    scheduler._execution.begin_drain()
    loop = asyncio.new_event_loop()

    async def _never_runs() -> bool:
        raise AssertionError("rejected work must not run")

    try:
        ok = scheduler._run_work(loop, _Person(), "scheduled", "cmd", _never_runs())
    finally:
        loop.close()

    assert ok is False
    # The drain means a stop is in progress; the worker mirrors it locally so
    # the rejection is treated as shutdown, not as a command error.
    assert scheduler._stop_event.is_set()


def test_run_work_lease_conflict_skips_without_stopping_scheduler(monkeypatch) -> None:
    import asyncio
    from contextlib import contextmanager

    from guildbotics.drivers.execution import WorkRejectedError

    scheduler = TaskScheduler(_Context(_Person()))
    loop = asyncio.new_event_loop()

    @contextmanager
    def reject_lease(**_kwargs):
        raise WorkRejectedError("busy", reason="lease_unavailable")
        yield

    async def _never_runs() -> bool:
        raise AssertionError("rejected work must not run")

    monkeypatch.setattr(scheduler._execution, "track_work", reject_lease)
    try:
        ok = scheduler._run_work(loop, _Person(), "routine", "cmd", _never_runs())
    finally:
        loop.close()

    assert ok is True
    assert not scheduler._stop_event.is_set()
