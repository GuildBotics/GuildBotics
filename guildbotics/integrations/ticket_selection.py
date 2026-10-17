"""Whether a ticket assigned to a member on the board is the member's work
now, whichever service keeps it.

A provider reads the ticket's lane, its comments, when the member was
assigned, and the pull requests that refer to it; :func:`select` decides
from those alone. Being assigned is how a human asks the member to act, so
only what was said since then shows whether the member already has. A ticket
in the ready lane is the member's unless the member spoke last and was not
summoned again; in the working lane it is taken up again when someone else
has spoken since, or when nobody has said anything yet. A pull request that
refers to the ticket takes it over: the pull request patrol follows it, and
its merge finishes the ticket.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence

from guildbotics.entities import Task
from guildbotics.entities.message import Message
from guildbotics.runtime.port import WorkflowStatus
from guildbotics.utils.timestamps import parse_iso_datetime


def since_assignment(comments: Sequence[Message], assigned_at: str) -> list[Message]:
    """The comments that count as evidence about the assigned work.

    Comments the member wrote earlier were written in another capacity -- as
    the ticket's author or a reviewer -- and must not suppress the run. The
    whole history is kept on the task for the agent to read; this narrower
    view only drives the selection decision. When the assignment time is not
    known, every comment is kept: an unknown request time must never be a
    reason to discard evidence and redo finished work.
    """
    since = parse_iso_datetime(assigned_at)
    if since is None:
        return list(comments)
    return [
        comment
        for comment in comments
        if (timestamp := parse_iso_datetime(comment.timestamp)) is None
        or timestamp >= since
    ]


def mention_pending(
    description: str, comments: Sequence[Message], mentions: Callable[[str], bool]
) -> bool:
    """Whether a mention of the member is still unanswered.

    A mention in the description or in any comment marks the member as
    summoned; the member's own next comment answers it.
    """
    pending = mentions(description)
    for comment in comments:
        if mentions(comment.content):
            pending = True
        if comment.author_type == Message.ASSISTANT:
            pending = False
    return pending


def linked_pull_state(pulls: Iterable[tuple[str, str]]) -> str | None:
    """The state (``merged`` for a merged one) of the pull request that refers
    to the ticket, of its ``(state, updated_at)`` pairs: an open one, and the
    latest updated; ``None`` when none does."""
    found = list(pulls)
    candidates = [pull for pull in found if pull[0] == "open"] or found
    return max(candidates, key=lambda pull: pull[1])[0] if candidates else None


async def select(
    task: Task,
    since: Sequence[Message],
    *,
    latest_status: WorkflowStatus | None,
    pending: bool,
    linked_pull: Callable[[], Awaitable[str | None]],
    done: Callable[[], Awaitable[object]],
) -> Task | None:
    """``task`` with why it is the member's work now, or ``None``.

    Args:
        task: The ticket, in its lane (``status``).
        since: Its comments since the assignment, oldest first.
        latest_status: The workflow status the last of them carries.
        pending: Whether a mention of the member is unanswered.
        linked_pull: The state of the pull request that refers to the ticket
            (:func:`linked_pull_state`).
        done: Moves the ticket to the done lane.
    """
    mine = bool(since) and since[-1].author_type == Message.ASSISTANT
    # The member's own failure or rate limit keeps the ticket out of selection.
    held = mine and latest_status is not None and latest_status.holds_selection()
    if task.status == Task.READY:
        if held or (mine and not pending):
            return None
        task.trigger_reason = "ready_lane"
        return task
    pull = await linked_pull()
    if pull is not None:
        # A merged PR finishes the ticket; an open one is followed on the PR
        # itself by the pull request patrol, never through the ticket.
        if pull == "merged":
            await done()
        return None
    if held or (since and mine):
        return None
    if since:
        task.trigger_reason = "issue_comment"
    else:
        task.trigger_reason = "issue_mention" if pending else "working_lane"
    return task
