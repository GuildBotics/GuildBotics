import datetime as dt
from types import SimpleNamespace

import pytest

from guildbotics.drivers import task_scheduler
from guildbotics.drivers.task_scheduler import TaskScheduler

EXPECTED_ROUTINE_CALL_COUNT = 2


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    # Keep the per-member chat dispatcher pointed at an empty temp workspace so it
    # is a no-op (no queued chat events) in these scheduler timing tests.
    monkeypatch.setenv("GUILDBOTICS_DATA_DIR", str(tmp_path / "data"))


class _Logger:
    def debug(self, message: str) -> None:
        return None

    def warning(self, message: str) -> None:
        return None

    def error(self, message: str) -> None:
        return None


class _Context:
    def __init__(self, member: object) -> None:
        self.team = SimpleNamespace(members=[member])
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
        ["routine"],
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
        ["routine"],
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


@pytest.mark.asyncio
async def test_process_pending_chat_delegates_to_dispatcher() -> None:
    scheduler = TaskScheduler(_Context(_Person()), [])
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

    scheduler = TaskScheduler(_Context(_Person()), [])
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

    scheduler = TaskScheduler(_Context(_Person()), [])
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
    scheduler = TaskScheduler(_Context(_Person()), [])
    scheduler._stop_event.set()

    count, should_stop = scheduler._update_consecutive_errors(
        False, source="scheduled", consecutive_errors=2
    )

    assert (count, should_stop) == (2, False)


def test_run_work_rejection_during_drain_mirrors_stop() -> None:
    import asyncio

    scheduler = TaskScheduler(_Context(_Person()), [])
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
