import pathlib
import sys

# Add project root to sys.path for test execution
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

import datetime
import random

from guildbotics.entities import ScheduledTask, Task

# Fixed random seed for tests
random.seed(42)


def make_scheduled_task(schedule: str):
    # build Task and pass it into ScheduledTask
    return ScheduledTask(
        task=Task(
            id="1",
            title="test",
            description="desc",
            workflow="wf",
            role="r",
        ),
        schedule=schedule,
    )


def test_should_run_simple_random(monkeypatch):
    # Mock sample_random to always return 5 minutes after 'now'
    now = datetime.datetime(2025, 5, 17, 12, 0)
    fixed_next = now + datetime.timedelta(minutes=5)
    monkeypatch.setattr(
        ScheduledTask, "_sample_random", lambda self, boundary, parts: fixed_next
    )
    # Mock croniter boundary to always return 'now'
    monkeypatch.setattr(
        "guildbotics.entities.task.croniter",
        lambda schedule, start: type("C", (), {"get_next": lambda self, t: now})(),
    )

    job = make_scheduled_task("? ? * * *")
    # next_random is fixed value (now + 5min)
    assert job._next_random == fixed_next

    # Before execution, should be False
    assert not job.should_run(now)
    # Should be True after next_random
    assert job.should_run(fixed_next)
    # Should not run consecutively
    assert not job.should_run(fixed_next)


def test_should_run_range(monkeypatch):
    # For ?(10-20) ?(3-5) * * *, control next_random to a fixed value for verification
    now = datetime.datetime(2025, 5, 17, 0, 0)
    fixed_boundary = now + datetime.timedelta(hours=4)
    fixed_next = fixed_boundary  # Fix sample time to boundary
    # Fix croniter boundary
    monkeypatch.setattr(
        "guildbotics.entities.task.croniter",
        lambda schedule, start: type(
            "C", (), {"get_next": lambda self, t: fixed_boundary}
        )(),
    )
    # Fix _sample_random to return boundary
    monkeypatch.setattr(
        ScheduledTask, "_sample_random", lambda self, boundary, parts: fixed_next
    )
    job = make_scheduled_task("?(10-20) ?(3-5) * * *")
    # next_random is in the future, so should not run now
    assert not job.should_run(now)
    # Should run after next_random
    assert job.should_run(fixed_next)


def test_should_run_boundary(monkeypatch):
    # 0 0 * * * → every day at 0:00
    now = datetime.datetime(2025, 5, 17, 0, 0)
    # Fix croniter boundary to always return 0:00
    monkeypatch.setattr(
        "guildbotics.entities.task.croniter",
        lambda schedule, start: type("C", (), {"get_next": lambda self, t: now})(),
    )
    job = make_scheduled_task("0 0 * * *")
    # next_random is 0:00
    assert job._next_random.hour == 0 and job._next_random.minute == 0
    # Should run at 0:00
    assert job.should_run(now)
    # Should not run at 0:01
    assert not job.should_run(datetime.datetime(2025, 5, 17, 0, 1))


def test_should_run_month_day_clamp(monkeypatch):
    # Check if a date outside the specified range (March 31) can be remapped to February
    # Fix croniter boundary: 2025-03-31 23:31
    fixed_boundary = datetime.datetime(2025, 3, 31, 23, 31)
    monkeypatch.setattr(
        "guildbotics.entities.task.croniter",
        lambda schedule, start: type(
            "C", (), {"get_next": lambda self, t: fixed_boundary}
        )(),
    )
    # Specify day-of-month as random range, fix month to February
    job = make_scheduled_task("31 23 * ?(2-2) *")
    # sample_random should change month field to 2 and clamp day to last day of February (28)
    assert job._next_random.month == 2
    assert job._next_random.day == 28


def test_should_run_weekday():
    # ? ? * * 0 → random time on Sunday
    job = make_scheduled_task("? ? * * 0")
    # next_random is Sunday
    assert job._next_random.weekday() == 6
    # Should run at that time
    assert job.should_run(job._next_random)


def test_invalid_schedule():
    # 4 fields should raise error
    import pytest

    with pytest.raises(ValueError):
        make_scheduled_task("* * * *")
    # 6 fields should also raise error
    with pytest.raises(ValueError):
        make_scheduled_task("* * * * * *")


def test_day_of_month_clamp_direct(monkeypatch):
    # Fix boundary to 2025-02-15 10:00 and verify clamp behavior for day-of-month field
    import calendar

    import guildbotics.entities.task as cj_module

    boundary = datetime.datetime(2025, 2, 15, 10, 0)
    # Fix croniter boundary
    monkeypatch.setattr(
        "guildbotics.entities.task.croniter",
        lambda schedule, start: type("C", (), {"get_next": lambda self, t: boundary})(),
    )
    # Fix random.randint to always return 31
    monkeypatch.setattr(cj_module.random, "randint", lambda a, b: 31)
    # Specify day-of-month field as ?(31-31)
    job = make_scheduled_task("* * ?(31-31) * *")
    # Should be clamped to last day of February (28)
    last_day = calendar.monthrange(boundary.year, boundary.month)[1]
    assert job._next_random.month == boundary.month
    assert job._next_random.day == last_day


def test_sample_random_weekday_range(monkeypatch):
    # Test range specification ?(2-4) for day-of-week field
    import guildbotics.entities.task as cj_module

    # Fix boundary to 2025-05-19 (Monday)
    boundary = datetime.datetime(2025, 5, 19, 12, 0)  # Mon
    monkeypatch.setattr(
        "guildbotics.entities.task.croniter",
        lambda schedule, start: type("C", (), {"get_next": lambda self, t: boundary})(),
    )
    # Fix random.randint to always return 4 (range 2-4)
    monkeypatch.setattr(cj_module.random, "randint", lambda a, b: 4)
    job = make_scheduled_task("* * * * ?(2-4)")
    # cron_cur = (weekday+1)%7 = (0+1)%7 = 1 → diff=(1-4)%7=4 → result=boundary-4days
    expected = boundary - datetime.timedelta(days=4)
    assert job._next_random == expected


def test_should_run_reset_cycle(monkeypatch):
    # Mock croniter to return two boundaries and verify reset after execution
    now = datetime.datetime(2025, 5, 17, 0, 0)
    later = now + datetime.timedelta(days=1)
    calls = {"count": 0}

    def fake_croniter(schedule, start):
        class C:
            def get_next(self_inner, t):
                calls["count"] += 1
                return now if calls["count"] == 1 else later

        return C()

    monkeypatch.setattr(
        "guildbotics.entities.task.croniter",
        fake_croniter,
    )
    # Fix sample_random to return boundary
    monkeypatch.setattr(
        ScheduledTask, "_sample_random", lambda self, boundary, parts: boundary
    )
    job = make_scheduled_task("0 0 * * *")
    # Should run at first boundary (= now)
    assert job.should_run(now)
    # Should not run again at the same time (executed=True triggers update & False)
    assert not job.should_run(now)
    # Should run again at next boundary (later)
    assert job.should_run(later)


def test_sample_random_default_range(monkeypatch):
    # Verify that sample_random applies default range (?) correctly
    import guildbotics.entities.task as cj_module

    # Fix boundary
    boundary = datetime.datetime(2025, 5, 17, 10, 20)
    monkeypatch.setattr(
        "guildbotics.entities.task.croniter",
        lambda schedule, start: type("C", (), {"get_next": lambda self, t: boundary})(),
    )
    # Fix random.randint to return 15 for minute, then 3 for hour
    seq = iter([15, 3])
    monkeypatch.setattr(cj_module.random, "randint", lambda a, b: next(seq))

    job = ScheduledTask(
        task=Task(
            id="1",
            title="test",
            description="desc",
            workflow="wf",
            role="r",
        ),
        schedule="? ? * * *",
    )
    # Next run time should be boundary with minute=15, hour=3
    expected = boundary.replace(minute=15).replace(hour=3)
    assert job._next_random == expected


def test_str_representation(monkeypatch):
    # Verify that __str__ format is as expected
    import guildbotics.entities.task as cj_module

    now = datetime.datetime(2025, 5, 20, 8, 30)
    next_rand = now + datetime.timedelta(hours=1)
    # Fix croniter boundary
    monkeypatch.setattr(
        "guildbotics.entities.task.croniter",
        lambda schedule, start: type("C", (), {"get_next": lambda self, t: now})(),
    )
    # Fix sample_random to return next_rand
    monkeypatch.setattr(
        cj_module.ScheduledTask,
        "_sample_random",
        lambda self, boundary, parts: next_rand,
    )
    job = make_scheduled_task("15 10 * * 2")
    # build expected string based on new ScheduledTask.__str__()
    expected = (
        f"ScheduledTask(task={job.task}, "
        f"schedule={job.schedule}, "
        f"next_run={next_rand}, "
        f"executed=False)"
    )
    assert str(job) == expected
