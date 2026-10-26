"""Working tickets answer their conversation before handing work to a PR."""

from unittest.mock import AsyncMock

import pytest

from guildbotics.entities import Task
from guildbotics.entities.message import Message
from guildbotics.integrations.ticket_selection import (
    mention_pending,
    relevant_comments,
    select,
    since_assignment,
)
from guildbotics.runtime.port import WorkflowStatus


@pytest.mark.asyncio
async def test_a_remark_to_someone_else_does_not_prevent_linked_pr_completion():
    own = Message(author_type=Message.ASSISTANT, content="Answered.")
    other = Message(author_type=Message.USER, content="@yuki Thanks.")
    since = relevant_comments([(own, False), (other, True)])
    task = Task(
        title="Work", description="", status=Task.IN_PROGRESS, comments=[own, other]
    )
    done = AsyncMock()
    assert (
        await select(
            task,
            since,
            latest_status=None,
            pending=False,
            linked_pull=AsyncMock(return_value="merged"),
            done=done,
        )
        is None
    )
    done.assert_awaited_once()
    assert task.comments == [own, other]


def test_the_members_own_answer_cannot_be_filtered_as_a_remark_to_someone_else():
    own = Message(author_type=Message.ASSISTANT, content="@yuki Context?")
    assert relevant_comments([(own, True)]) == [own]


@pytest.mark.asyncio
@pytest.mark.parametrize("pull", [None, "open", "merged"])
@pytest.mark.parametrize(
    ("conversation", "expected"),
    [
        ("silent", "working_lane"),
        ("mine", None),
        ("other", "issue_comment"),
        ("mention", "issue_mention"),
        ("mention_after_mine", "issue_mention"),
        ("failed", None),
        ("rate_limited", None),
    ],
)
async def test_working_ticket_conversation_precedes_its_linked_pr(
    pull, conversation, expected
):
    task = Task(title="Work", description="", status=Task.IN_PROGRESS)
    since = []
    status = None
    pending = conversation in {"mention", "mention_after_mine"}
    if conversation in {
        "mine",
        "other",
        "mention_after_mine",
        "failed",
        "rate_limited",
    }:
        since.append(Message(author_type=Message.ASSISTANT))
    if conversation == "other":
        since.append(Message(author_type=Message.USER))
    if conversation in {"failed", "rate_limited"}:
        status = WorkflowStatus(
            reason=conversation,
            person_id="aiko",
            run_id="run-1",
            retry_after_at="2099-01-01T00:00:00+00:00",
        )
        # A mention must not override the member's own held status.
        pending = True
    linked_pull = AsyncMock(return_value=pull)
    done = AsyncMock()

    selected = await select(
        task,
        since,
        latest_status=status,
        pending=pending,
        linked_pull=linked_pull,
        done=done,
    )

    answers = expected in {"issue_comment", "issue_mention"}
    reason = expected if answers or pull is None else None
    assert selected is (task if reason else None)
    assert task.trigger_reason == reason
    assert done.await_count == int(pull == "merged" and not answers and status is None)
    assert linked_pull.await_count == int(not answers and status is None)


@pytest.mark.asyncio
@pytest.mark.parametrize("pull", [None, "open", "merged"])
async def test_only_comments_since_assignment_resume_a_working_ticket(pull):
    comments = [
        Message(
            author_type=Message.USER,
            content="@aiko earlier request",
            timestamp="2026-01-01T00:00:00+00:00",
        ),
        Message(
            author_type=Message.ASSISTANT,
            timestamp="2026-01-02T00:00:00+00:00",
        ),
    ]
    since = since_assignment(comments, "2026-01-02T00:00:00+00:00")
    assert since == comments[1:]
    task = Task(title="Work", description="", status=Task.IN_PROGRESS)
    done = AsyncMock()

    assert (
        await select(
            task,
            since,
            latest_status=None,
            pending=mention_pending(
                False, ((comment, "@aiko" in comment.content) for comment in since)
            ),
            linked_pull=AsyncMock(return_value=pull),
            done=done,
        )
        is None
    )
    assert done.await_count == int(pull == "merged")


@pytest.mark.parametrize(
    ("described", "comments", "expected"),
    [
        (False, [], False),
        (True, [], True),
        (True, [(Message.USER, False)], True),
        (False, [(Message.USER, True)], True),
        (False, [(Message.ASSISTANT, True)], False),
        (True, [(Message.ASSISTANT, False)], False),
        (False, [(Message.USER, True), (Message.ASSISTANT, True)], False),
        (True, [(Message.ASSISTANT, False), (Message.USER, False)], False),
        (True, [(Message.ASSISTANT, False), (Message.USER, True)], True),
    ],
)
def test_resolved_mentions_are_answered_by_the_members_next_comment(
    described, comments, expected
):
    # Content deliberately disagrees with the flags: resolution is the provider's job.
    messages = (
        (
            Message(author_type=author, content="No summons" if mentioned else "@aiko"),
            mentioned,
        )
        for author, mentioned in comments
    )
    assert mention_pending(described, messages) is expected
