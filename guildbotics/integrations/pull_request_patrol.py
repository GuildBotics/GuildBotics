"""What an open pull request asks of one member, whichever code host keeps it.

Pull requests reach a member through the two roles the code host records on
the PR itself: the member wrote it, or the member reviews it. A provider
reads one PR into a :class:`PullRequest` snapshot; the decision is made here
from that snapshot alone.

Both roles read everything someone else says before deciding what it asks
for, so the patrol does not judge a statement by its kind. A statement is an
unresolved review thread whose last word is someone else's, a review with a
body in any state (an approval may carry a suggestion), or a conversation
comment. The member has answered it with a later comment, review, or thread
reply, or with a reaction on it. A review without a body has nothing to read;
its inline comments are threads.

Author role (``pull_request_feedback``): a statement the member has not
answered.

Reviewer role (``pull_request_review``): the member is a requested reviewer,
new commits landed after the member's last review, or, once the member has
spoken on the PR, there is a statement the member has not answered (threads
only those the member took part in). Re-reviews not driven by a request stop
after :data:`MAX_REVIEW_ROUNDS` rounds (``REVIEW_LIMIT``): the code host makes
the PR a draft and then says so on it (``REVIEW_LIMIT_REASON``). Rounds are
counted from the last time a human marked the PR ready for review, as the code
host records it, so every reviewer starts a new count; nothing else is stored.
A review-limit notice by any member also counts as that point, so a PR
announced before the limit made it a draft resumes too.

Checks role (``pull_request_checks``): the member's run on its own PR stopped
to await the head's checks (its latest status notice there is
``awaiting_checks``), and they have settled -- no ``checks_pending`` blocker
is left -- or the head moved past the one it awaited. The wait lasts until the
member's next status notice on the PR: a later wait, a failure, or the notice
the host leaves when the resumed run ends otherwise
(:func:`checks_settled`), so no comment a run writes or leaves out can end it
early or keep it forever.

Workflow status notices (rate limit, failure, review limit, awaiting and
settled checks) are neither statements nor answers: they only suppress
selection, mark the limit, or await the checks. A human marking the PR ready for review also ends
every earlier hold.

A draft PR reaches neither role. Draft is the switch a human flips to take a
PR into their own hands, and the code host leaves drafts out of the patrol
before any snapshot is loaded. Besides humans, only the code host changes it,
and only to hand a PR over at the review limit; a member's turn cannot.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from guildbotics.entities import Task
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.port import ServiceError, WorkflowStatus, WorkflowStatusReason
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.timestamps import parse_iso_datetime

FEEDBACK = "pull_request_feedback"
REVIEW = "pull_request_review"
REVIEW_LIMIT = "pull_request_review_limit"
REVIEW_LIMIT_REASON: WorkflowStatusReason = "review_limit"
CHECKS = "pull_request_checks"
AWAITING_CHECKS_REASON: WorkflowStatusReason = "awaiting_checks"
CHECKS_SETTLED_REASON: WorkflowStatusReason = "checks_settled"
MAX_REVIEW_ROUNDS = 3

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Review:
    """One submitted review; a code host may record a thread reply as one."""

    author: str
    body: str
    submitted_at: str
    commit_oid: str
    reply_only: bool
    reactors: frozenset[str]


@dataclass(frozen=True)
class Comment:
    """One conversation comment on the pull request."""

    author: str
    created_at: str
    reactors: frozenset[str]
    #: The workflow status the comment carries, if it is a notice.
    status: WorkflowStatus | None


@dataclass(frozen=True)
class ReviewThread:
    resolved: bool
    participants: frozenset[str]
    last_author: str
    last_created_at: str
    last_reactors: frozenset[str]


@dataclass(frozen=True)
class PullRequest:
    """One pull request as the patrol sees it; names are in the form the code
    host compares them in, and times compare as strings."""

    #: What the code host names the PR by, as the task's id.
    id: str
    label: str
    url: str
    title: str
    body: str
    open: bool
    is_draft: bool
    created_at: str
    author: str
    head_oid: str
    ready_at: str
    requested_reviewers: frozenset[str]
    reviews: tuple[Review, ...]
    comments: tuple[Comment, ...]
    threads: tuple[ReviewThread, ...]


def _has_unanswered_thread(pr: PullRequest, me: str, *, mine_only: bool) -> bool:
    """An unresolved thread waits on the member when someone else spoke last.

    A reaction from the member on that last comment counts as an answer.
    ``mine_only`` restricts the question to threads the member took part in.
    """
    return any(
        not thread.resolved
        and thread.last_author != me
        and me not in thread.last_reactors
        and (not mine_only or me in thread.participants)
        for thread in pr.threads
    )


def _last_spoken_at(pr: PullRequest, me: str) -> str:
    """When the member last commented, reviewed, or replied in a thread.

    A thread reply is recorded as a review, so the reviews cover it.
    """
    return max(
        [
            comment.created_at
            for comment in pr.comments
            if comment.author == me and comment.status is None
        ]
        + [review.submitted_at for review in pr.reviews if review.author == me],
        default="",
    )


def _has_unanswered_statement(pr: PullRequest, me: str) -> bool:
    """A review body or conversation comment by someone else waits on the member.

    Each is answered on its own: by anything the member said after it, or by
    the member's reaction on it.
    """
    spoken = _last_spoken_at(pr, me)
    statements = [
        (comment.created_at, comment.reactors)
        for comment in pr.comments
        if comment.author != me and comment.status is None
    ] + [
        (review.submitted_at, review.reactors)
        for review in pr.reviews
        if review.author != me and review.body.strip()
    ]
    return any(at > spoken and me not in reactors for at, reactors in statements)


def _latest_activity_by_others(pr: PullRequest, me: str) -> str:
    """When someone other than the member last did anything on the PR.

    Another member's status notice is not acting on the PR: counting it would
    let two members whose runs keep failing lift each other's hold forever.
    """
    return max(
        [
            comment.created_at
            for comment in pr.comments
            if comment.author != me and comment.status is None
        ]
        + [review.submitted_at for review in pr.reviews if review.author != me]
        + [thread.last_created_at for thread in pr.threads if thread.last_author != me],
        default="",
    )


def _is_suppressed(pr: PullRequest, me: str) -> bool:
    """The member's latest comment is a failure or rate-limit notice that
    nobody has acted on since.

    Any later activity by someone else (a conversation comment, a review, a
    reply in a thread) lifts the notice, so a thread reply can restart work
    that a failure put on hold. So does a human marking the PR ready for
    review later: that restarts every member, as with the rounds, including
    one whose run failed while the PR was a draft. Another member's
    review-limit notice does not: like any status notice, it is not acting
    on the PR.
    """
    mine = [comment for comment in pr.comments if comment.author == me]
    if not mine:
        return False
    latest = mine[-1]
    if latest.status is None or not latest.status.holds_selection():
        return False
    lifted_at = max(_latest_activity_by_others(pr, me), pr.ready_at)
    return lifted_at <= latest.created_at


def _reviewed_heads(pr: PullRequest, me: str, since: str = "") -> set[str]:
    """Head commits the member reviewed after *since*, excluding reply-only
    submissions.

    A thread reply recorded as a review at the current head would both
    inflate the rounds and hide the commits it landed on from the re-review
    check.
    """
    return {
        review.commit_oid
        for review in pr.reviews
        if review.author == me
        and not review.reply_only
        and review.commit_oid
        and review.submitted_at > since
    }


def _restarted_at(pr: PullRequest) -> str:
    """When the rounds start over: the PR was last handed back to the members.

    That is a human marking it ready for review, read from the code host
    rather than inferred from a notice: a notice can land before or after
    another member's run fails, while the human's decision comes after both.
    A review-limit notice by any member also counts, for PRs announced before
    the limit made them a draft.
    """
    return max(
        [pr.ready_at]
        + [
            comment.created_at
            for comment in pr.comments
            if comment.status is not None
            and comment.status.reason == REVIEW_LIMIT_REASON
        ],
        default="",
    )


def review_rounds(pr: PullRequest, me: str) -> set[str]:
    """Head commits the member reviewed since the PR was last handed back.

    Only a human makes a draft ready for review again. That decision is about
    the PR, not about the member who reached the limit, so every reviewer's
    count starts over, whoever or whatever made it a draft.
    """
    return _reviewed_heads(pr, me, _restarted_at(pr))


def _review_work(pr: PullRequest, me: str) -> str | None:
    if me in pr.requested_reviewers:
        return REVIEW
    reviewed = _reviewed_heads(pr, me)
    if not (
        (reviewed and pr.head_oid not in reviewed)
        or _has_unanswered_thread(pr, me, mine_only=True)
        or (_last_spoken_at(pr, me) and _has_unanswered_statement(pr, me))
    ):
        return None
    return REVIEW if len(review_rounds(pr, me)) < MAX_REVIEW_ROUNDS else REVIEW_LIMIT


def pull_request_work(pr: PullRequest, me: str) -> str | None:
    """Return what *pr* asks of the member *me*, or ``None``.

    Args:
        pr: The pull request snapshot.
        me: The member's name, in the form the snapshot's names are in.

    Returns:
        ``FEEDBACK`` or ``REVIEW`` for work to dispatch, ``REVIEW_LIMIT`` when
        the re-review budget is exhausted, else ``None``.
    """
    if not pr.open or pr.is_draft or _is_suppressed(pr, me):
        return None
    if pr.author == me:
        return (
            FEEDBACK
            if _has_unanswered_thread(pr, me, mine_only=False)
            or _has_unanswered_statement(pr, me)
            else None
        )
    return _review_work(pr, me)


def _awaited_checks(pr: PullRequest, me: str) -> WorkflowStatus | None:
    """The checks the member's run on its own open PR stopped to await: its
    latest status notice there says so; ``None`` otherwise. A rate limit
    that has reset no longer stands between: the run it stopped was resumed
    for the checks, which it must be again."""
    notices = [
        comment.status
        for comment in pr.comments
        if comment.author == me
        and comment.status is not None
        and (
            comment.status.reason != "rate_limited" or comment.status.holds_selection()
        )
    ]
    if (
        not notices
        or notices[-1].reason != AWAITING_CHECKS_REASON
        or pr.author != me
        or not pr.open
        or pr.is_draft
        or not pr.head_oid
    ):
        return None
    return notices[-1]


async def _checks_settled(
    code: CodeHostingService, pr: PullRequest, awaited: WorkflowStatus
) -> bool:
    """Whether what the run awaits is over: the head moved, or the code host
    no longer reports its checks pending. A read that fails tells nothing, so
    the next patrol asks again."""
    if pr.head_oid != awaited.head:
        return True
    try:
        readiness = await code.readiness(pr.url)
    except ServiceError as exc:
        _LOGGER.warning(f"Could not read the checks of {pr.url}: {exc}")
        return False
    return all(
        blocker.code != "checks_pending" for blocker in readiness.completion_blockers
    )


async def patrol(
    code: CodeHostingService,
    pr: PullRequest,
    me: str,
    *,
    provider: str,
    person_id: str,
    draft: Callable[[], Awaitable[object]],
) -> Task | None:
    """The task *pr* is for the member *me*; one at the re-review limit is
    handed over instead (:func:`hand_over_at_review_limit`). Only the checks
    role reads the code host beyond the snapshot, and only for a PR whose
    member awaits its checks at the head the snapshot has.

    Args:
        code: The code host the PR is on.
        pr: The pull request snapshot.
        me: The member's name, in the form the snapshot's names are in.
        provider: The provider's name, as the task records it.
        person_id: The member.
        draft: Makes the PR a draft.
    """
    work = pull_request_work(pr, me)
    if work is None:
        awaited = _awaited_checks(pr, me)
        if awaited is None or not await _checks_settled(code, pr, awaited):
            return None
        work = CHECKS
    if work == REVIEW_LIMIT:
        await hand_over_at_review_limit(code, pr.url, person_id, draft)
        return None
    return Task(
        provider=provider,
        label=pr.label,
        id=pr.id,
        url=pr.url,
        title=pr.title,
        description=pr.body,
        status=Task.IN_PROGRESS,
        created_at=parse_iso_datetime(pr.created_at),
        assignee=person_id,
        pull_request_url=pr.url,
        trigger_reason=work,
    )


async def hand_over_at_review_limit(
    code: CodeHostingService,
    url: str,
    person_id: str,
    draft: Callable[[], Awaitable[object]],
) -> None:
    """Make the PR a draft, then say on it that automatic re-review stopped.

    The notice tells the human that the PR is theirs until they mark it
    ready for review, so it is posted only once the PR is a draft. When
    the conversion fails, a failure notice takes its place: it holds the
    PR until someone acts on it, and the rounds stay as they are.
    """
    reason: WorkflowStatusReason = REVIEW_LIMIT_REASON
    try:
        await draft()
    except Exception as exc:
        _LOGGER.warning(f"Could not convert {url} to a draft: {exc}")
        reason = "failed"
    notice = (
        t(
            "integrations.pull_request_patrol.review_limit_reached",
            count=MAX_REVIEW_ROUNDS,
        )
        if reason == REVIEW_LIMIT_REASON
        else t(
            "integrations.pull_request_patrol.review_limit_draft_failed",
            count=MAX_REVIEW_ROUNDS,
        )
    )
    await code.comment(
        url,
        notice,
        status=WorkflowStatus(
            reason=reason, person_id=person_id, run_id="", subject=url
        ),
    )


async def await_checks(
    code: CodeHostingService,
    url: str,
    *,
    person_id: str,
    run_id: str,
    subject: str,
    head: str,
) -> None:
    """Say on the PR that the member's run stopped to await the checks of
    ``head``; the patrol resumes the member once they settle."""
    await code.comment(
        url,
        t("integrations.pull_request_patrol.awaiting_checks"),
        status=WorkflowStatus(
            reason=AWAITING_CHECKS_REASON,
            person_id=person_id,
            run_id=run_id,
            subject=subject,
            head=head,
        ),
    )


async def checks_settled(
    code: CodeHostingService, url: str, *, person_id: str, run_id: str
) -> None:
    """End the wait on the PR: the run resumed for its checks ended without
    awaiting them again."""
    await code.comment(
        url,
        t("integrations.pull_request_patrol.checks_settled"),
        status=WorkflowStatus(
            reason=CHECKS_SETTLED_REASON,
            person_id=person_id,
            run_id=run_id,
            subject=url,
        ),
    )
