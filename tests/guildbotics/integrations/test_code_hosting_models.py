"""Unit tests for integrations.code_hosting_service data models.

This suite verifies sorting, serialization, reply detection, and merging behaviors
for InlineComment, InlineCommentThread, ReviewComment, and ReviewComments.
"""

from guildbotics.entities.message import Message
from guildbotics.integrations.code_hosting_service import (
    InlineComment,
    InlineCommentThread,
    ReviewComment,
    ReviewComments,
)


def test_inline_comment_sort_and_to_simple_dict():
    """InlineComment orders by path, then line, then created_at; to_simple_dict maps role."""
    c1 = InlineComment(
        path="a.py",
        line=10,
        body="Third",
        comment_id=3,
        author="Charlie",
        created_at="2024-01-01T00:00:02Z",
        is_reviewee=False,
    )
    c2 = InlineComment(
        path="a.py",
        line=5,
        body="First",
        comment_id=1,
        author="Alice",
        created_at="2024-01-01T00:00:01Z",
        is_reviewee=False,
    )
    c3 = InlineComment(
        path="b.py",
        line=1,
        body="Fourth",
        comment_id=4,
        author="Dave",
        created_at="2024-01-01T00:00:00Z",
        is_reviewee=True,
    )
    # Same path/line as c2 but later creation time to test tie-breaker
    c4 = InlineComment(
        path="a.py",
        line=5,
        body="Second",
        comment_id=2,
        author="Bob",
        created_at="2024-01-01T00:00:03Z",
        is_reviewee=False,
    )

    comments = [c1, c2, c3, c4]
    comments.sort()

    # Expect order: a.py:5 (earlier), a.py:5 (later), a.py:10, b.py:1
    assert [(c.path, c.line, c.body) for c in comments] == [
        ("a.py", 5, "First"),
        ("a.py", 5, "Second"),
        ("a.py", 10, "Third"),
        ("b.py", 1, "Fourth"),
    ]

    # to_simple_dict maps reviewer to User, reviewee to Assistant
    assert c2.to_simple_dict() == {Message.USER: "First"}
    assert c3.to_simple_dict() == {Message.ASSISTANT: "Fourth"}


def test_inline_comment_thread_is_replied_add_reply_to_dict_and_str(monkeypatch):
    """InlineCommentThread reply detection, append behavior, dict + string views."""
    first = InlineComment(
        path="src/app.py",
        line=42,
        body="Please rename this variable.",
        comment_id=10,
        author="Alice",
        created_at="2024-02-01T10:00:00Z",
        is_reviewee=False,
        line_content="answer = compute()",
    )
    thread = InlineCommentThread(path=first.path, line=first.line, comments=[first])

    # Initially unreplied because the last comment is from reviewer
    assert thread.is_replied() is False

    # Make reply deterministic
    monkeypatch.setattr(
        "guildbotics.integrations.code_hosting_service.time.strftime",
        lambda fmt: "2025-01-01T00:00:00Z",
    )
    thread.add_reply("Renamed to result, thanks!")

    assert thread.is_replied() is True
    assert thread.reply == "Renamed to result, thanks!"
    assert len(thread.comments) == 2
    last = thread.comments[-1]
    assert last.is_reviewee is True
    assert last.author == "Reviewee (myself)"
    assert last.created_at == "2025-01-01T00:00:00Z"

    # to_dict reflects path/line/line_content and simplified comments
    d = thread.to_dict()
    assert d["path"] == "src/app.py"
    assert d["line"] == 42
    assert d["line_content"] == "answer = compute()"
    assert d["comments"] == [
        {Message.USER: "Please rename this variable."},
        {Message.ASSISTANT: "Renamed to result, thanks!"},
    ]

    # __str__ contains header and both author:body lines
    s = str(thread)
    assert "**File:** src/app.py" in s
    assert "**Line:** 42" in s
    assert "```" in s
    assert "Alice: Please rename this variable." in s
    assert "Reviewee (myself): Renamed to result, thanks!" in s


def test_review_comment_sort_and_to_simple_dict():
    """ReviewComment sorts by created_at and serializes role."""
    r1 = ReviewComment(
        body="Looks good overall.",
        author="Bob",
        created_at="2024-01-01T10:00:01Z",
        is_reviewee=False,
    )
    r2 = ReviewComment(
        body="Thanks! Merged.",
        author="Reviewee (myself)",
        created_at="2024-01-01T10:00:02Z",
        is_reviewee=True,
    )
    comments = [r2, r1]
    comments.sort()
    assert [c.body for c in comments] == ["Looks good overall.", "Thanks! Merged."]
    assert r1.to_simple_dict() == {Message.USER: "Looks good overall."}
    assert r2.to_simple_dict() == {Message.ASSISTANT: "Thanks! Merged."}


def test_review_comments_merge_filter_is_replied_and_to_simple_dicts():
    """ReviewComments merges threads, filters replied ones, computes is_replied, and flattens review comments."""
    # Review comments (intentionally out of order)
    rc2 = ReviewComment(
        body="Acknowledged.",
        author="Reviewee (myself)",
        created_at="2024-03-01T12:00:01Z",
        is_reviewee=True,
    )
    rc1 = ReviewComment(
        body="Please update docs.",
        author="Carol",
        created_at="2024-03-01T12:00:00Z",
        is_reviewee=False,
    )

    # Inline comments: one fully replied thread (src/a.py:5), one pending (src/b.py:3)
    ic_a1 = InlineComment(
        path="src/a.py",
        line=5,
        body="Nit: trailing space.",
        comment_id=101,
        author="Carol",
        created_at="2024-03-01T11:59:58Z",
        is_reviewee=False,
        line_content="print(value) ",
    )
    ic_a2 = InlineComment(
        path="src/a.py",
        line=5,
        body="Fixed.",
        comment_id=102,
        author="Reviewee (myself)",
        created_at="2024-03-01T11:59:59Z",
        is_reviewee=True,
    )
    ic_b1 = InlineComment(
        path="src/b.py",
        line=3,
        body="Consider extracting a function.",
        comment_id=103,
        author="Carol",
        created_at="2024-03-01T12:00:00Z",
        is_reviewee=False,
        line_content="x = y + z",
    )

    review_comments = [rc2, rc1]
    inline_comments = [ic_b1, ic_a2, ic_a1]

    # include_all_comments=False: replied threads (a.py:5) are filtered out
    rc = ReviewComments(
        review_comments=review_comments, inline_comments=inline_comments
    )

    # Review comments are sorted and preserved
    assert [c.body for c in rc.review_comments] == [
        "Please update docs.",
        "Acknowledged.",
    ]

    # Only the unreplied thread remains (src/b.py:3)
    assert len(rc.inline_comment_threads) == 1
    t = rc.inline_comment_threads[0]
    assert (t.path, t.line) == ("src/b.py", 3)
    assert t.comments[0].body == "Consider extracting a function."

    # Overall is_replied is False because an inline thread is pending
    assert rc.is_replied is False

    # to_simple_dicts flattens sorted review comments
    assert rc.to_simple_dicts() == [
        {Message.USER: "Please update docs."},
        {Message.ASSISTANT: "Acknowledged."},
    ]

    # include_all_comments=True retains both threads
    rc_all = ReviewComments(
        review_comments=review_comments,
        inline_comments=inline_comments,
        include_all_comments=True,
    )
    assert len(rc_all.inline_comment_threads) == 2
    assert {(t.path, t.line) for t in rc_all.inline_comment_threads} == {
        ("src/a.py", 5),
        ("src/b.py", 3),
    }


def test_review_comments_is_replied_when_no_comments():
    """When there are no comments, ReviewComments reports replied."""
    rc = ReviewComments(review_comments=[], inline_comments=[])
    assert rc.is_replied is True
