import datetime as dt
from types import SimpleNamespace

from guildbotics.drivers import task_scheduler
from guildbotics.drivers.task_scheduler import TaskScheduler

EXPECTED_ROUTINE_CALL_COUNT = 2


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
    def __init__(self) -> None:
        self.person_id = "alice"
        self.is_active = True
        self.routine_commands: list[str] = []

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

    person = _Person()
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

    person = _Person()
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
