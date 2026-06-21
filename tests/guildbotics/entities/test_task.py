import pathlib
import sys

# Add project root to sys.path for test execution
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

import datetime as dt

from guildbotics.entities import Task


def make_task(
    *,
    priority: int | None = None,
    due_date: dt.datetime | None = None,
    created_at: dt.datetime | None = None,
) -> Task:
    return Task(
        id="t",
        title="title",
        description="desc",
        workflow="wf",
        priority=priority,
        due_date=due_date,
        created_at=created_at,
    )


def test_lt_compares_priority_first():
    a = make_task(priority=1)
    b = make_task(priority=2)
    assert a < b
    assert not (b < a)


def test_lt_missing_priority_treated_as_lowest_priority():
    a = make_task(priority=None)
    b = make_task(priority=5)
    assert b < a
    assert not (a < b)


def test_lt_compares_due_date_with_tz_normalization():
    # Same priority, different due_date (naive treated as UTC)
    p = 1
    d1 = dt.datetime(2025, 1, 1, 12, 0)  # naive -> UTC
    d2 = dt.datetime(2025, 1, 1, 12, 30, tzinfo=dt.UTC)
    a = make_task(priority=p, due_date=d1)
    b = make_task(priority=p, due_date=d2)
    assert a < b


def test_lt_compares_due_date_with_mixed_timezones():
    # JST vs UTC; 12:00+09:00 == 03:00Z < 03:30Z
    p = 1
    d1 = dt.datetime(2025, 1, 1, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    d2 = dt.datetime(2025, 1, 1, 3, 30, tzinfo=dt.UTC)
    a = make_task(priority=p, due_date=d1)
    b = make_task(priority=p, due_date=d2)
    assert a < b


def test_lt_due_date_none_considered_last():
    p = 1
    a = make_task(priority=p, due_date=dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.UTC))
    b = make_task(priority=p, due_date=None)
    assert a < b


def test_lt_uses_created_at_as_tiebreaker():
    p = 1
    due = dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.UTC)
    c1 = dt.datetime(2024, 12, 31, 23, 55)  # naive -> UTC
    c2 = dt.datetime(2024, 12, 31, 23, 59, tzinfo=dt.UTC)
    a = make_task(priority=p, due_date=due, created_at=c1)
    b = make_task(priority=p, due_date=due, created_at=c2)
    assert a < b
