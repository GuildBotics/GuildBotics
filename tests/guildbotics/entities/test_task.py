import pathlib
import sys

# Add project root to sys.path for test execution
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

import datetime as dt

from guildbotics.entities import Task


def make_task(
    *,
    created_at: dt.datetime | None = None,
) -> Task:
    return Task(
        id="t",
        title="title",
        description="desc",
        workflow="wf",
        created_at=created_at,
    )


def test_lt_orders_older_created_at_first():
    a = make_task(created_at=dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.UTC))
    b = make_task(created_at=dt.datetime(2025, 1, 2, 0, 0, tzinfo=dt.UTC))
    assert a < b
    assert not (b < a)


def test_lt_treats_naive_created_at_as_utc():
    a = make_task(created_at=dt.datetime(2025, 1, 1, 12, 0))  # naive -> UTC
    b = make_task(created_at=dt.datetime(2025, 1, 1, 12, 30, tzinfo=dt.UTC))
    assert a < b


def test_lt_normalizes_mixed_timezones():
    # JST vs UTC; 12:00+09:00 == 03:00Z < 03:30Z
    a = make_task(
        created_at=dt.datetime(
            2025, 1, 1, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))
        )
    )
    b = make_task(created_at=dt.datetime(2025, 1, 1, 3, 30, tzinfo=dt.UTC))
    assert a < b


def test_lt_created_at_none_considered_last():
    a = make_task(created_at=dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.UTC))
    b = make_task(created_at=None)
    assert a < b
    assert not (b < a)


def test_task_has_no_priority_or_due_date_fields():
    """Ordering inputs are limited to the fields the board actually fills in."""
    assert "priority" not in Task.model_fields
    assert "due_date" not in Task.model_fields
