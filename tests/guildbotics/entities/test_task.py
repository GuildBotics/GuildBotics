import pathlib
import sys

# Add project root to sys.path for test execution
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

import datetime as dt

import pytest

from guildbotics.entities import Message, Task


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
        role="role",
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
    d2 = dt.datetime(2025, 1, 1, 12, 30, tzinfo=dt.timezone.utc)
    a = make_task(priority=p, due_date=d1)
    b = make_task(priority=p, due_date=d2)
    assert a < b


def test_lt_compares_due_date_with_mixed_timezones():
    # JST vs UTC; 12:00+09:00 == 03:00Z < 03:30Z
    p = 1
    d1 = dt.datetime(2025, 1, 1, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    d2 = dt.datetime(2025, 1, 1, 3, 30, tzinfo=dt.timezone.utc)
    a = make_task(priority=p, due_date=d1)
    b = make_task(priority=p, due_date=d2)
    assert a < b


def test_lt_due_date_none_considered_last():
    p = 1
    a = make_task(
        priority=p, due_date=dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.timezone.utc)
    )
    b = make_task(priority=p, due_date=None)
    assert a < b


def test_lt_uses_created_at_as_tiebreaker():
    p = 1
    due = dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.timezone.utc)
    c1 = dt.datetime(2024, 12, 31, 23, 55)  # naive -> UTC
    c2 = dt.datetime(2024, 12, 31, 23, 59, tzinfo=dt.timezone.utc)
    a = make_task(priority=p, due_date=due, created_at=c1)
    b = make_task(priority=p, due_date=due, created_at=c2)
    assert a < b


def test_find_output_title_and_url_from_comments_normal():
    comments = [
        Message(content="User says hi", author="u", author_type=Message.USER),
        Message(
            content=(
                "Some analysis\n"
                "Output: [My Result](https://example.com/page)\n"
                "More lines"
            ),
            author="bot",
            author_type=Message.ASSISTANT,
        ),
    ]
    t = Task(
        id="1",
        title="title",
        description="desc",
        comments=comments,
        workflow="wf",
        role="role",
    )
    title, url = t.find_output_title_and_url_from_comments(strict=True)
    assert title == "My Result"
    assert url == "https://example.com/page"


def test_find_output_title_and_url_not_found_strict_false_returns_empty():
    comments = [
        Message(content="Nothing here", author="u", author_type=Message.USER),
        Message(
            content="Assistant without output",
            author="bot",
            author_type=Message.ASSISTANT,
        ),
    ]
    t = Task(
        id="1",
        title="title",
        description="desc",
        comments=comments,
        workflow="wf",
        role="role",
    )
    title, url = t.find_output_title_and_url_from_comments(strict=False)
    assert (title, url) == ("", "")


def test_find_output_title_and_url_not_found_strict_true_raises():
    comments = [
        Message(content="No output line", author="bot", author_type=Message.ASSISTANT),
    ]
    t = Task(
        id="1",
        title="title",
        description="desc",
        comments=comments,
        workflow="wf",
        role="role",
    )
    with pytest.raises(ValueError):
        t.find_output_title_and_url_from_comments(strict=True)
