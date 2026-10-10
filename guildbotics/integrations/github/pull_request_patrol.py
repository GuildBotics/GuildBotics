"""What an open pull request asks of one member.

The project board only knows issues, so pull requests reach a member through
the two roles GitHub records on the PR itself: the member wrote it, or the
member reviews it. This module holds the GraphQL shape of one PR, its parsing,
and the decision made from that snapshot; fetching and dispatching live in
:class:`~guildbotics.integrations.github.github_ticket_manager.GitHubTicketManager`.

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
after :data:`MAX_REVIEW_ROUNDS` rounds (``REVIEW_LIMIT``): the manager makes
the PR a draft and then says so on it (``REVIEW_LIMIT_REASON``). Rounds are
counted from the last time a human marked the PR ready for review, as GitHub
records it, so every reviewer starts a new count; nothing else is stored. A
review-limit notice by any member also counts as that point, so a PR
announced before the limit made it a draft resumes too.

Workflow status notices (rate limit, failure, review limit) are neither
statements nor answers: they only suppress selection or mark the limit. A
human marking the PR ready for review also ends every earlier hold.

A draft PR reaches neither role. Draft is the switch a human flips to take a
PR into their own hands, and the manager's search excludes drafts before any
snapshot is loaded. Besides humans, only the manager changes it, and only to
hand a PR over at the review limit; a member's turn cannot.
"""

from __future__ import annotations

from dataclasses import dataclass

from guildbotics.integrations.github.github_utils import normalize_login
from guildbotics.integrations.github.workflow_status_comment import (
    parse_workflow_status_comment,
    suppresses_ticket_selection,
)

FEEDBACK = "pull_request_feedback"
REVIEW = "pull_request_review"
REVIEW_LIMIT = "pull_request_review_limit"
REVIEW_LIMIT_REASON = "review_limit"
MAX_REVIEW_ROUNDS = 3

PULL_REQUEST_FEEDBACK_SOURCE_QUERIES = {
    "conversation_comments": "comments(last: 100)",
    "review_summaries": "reviews(last: 100)",
    "review_threads": "reviewThreads(first: 100)",
}
PULL_REQUEST_FEEDBACK_SOURCES = frozenset(PULL_REQUEST_FEEDBACK_SOURCE_QUERIES)

# A query names the fields it reads, and a pull request is identified by the
# same ones an issue is (the ticket manager's project query reads them too).
# The selection sets of two queries of different types are not shared logic.
# pylint: disable=duplicate-code
PULL_REQUEST_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      id
      number
      url
      title
      body
      state
      isDraft
      createdAt
      headRefOid
      author { login }
      reviewRequests(first: 50) {
        nodes { requestedReviewer { ... on User { login } } }
      }
      reviews(last: 100) {
        nodes {
          author { login }
          state
          body
          submittedAt
          commit { oid }
          comments(first: 100) { nodes { replyTo { id } } }
          reactionGroups { reactors(first: 100) { nodes { ... on Actor { login } } } }
        }
      }
      comments(last: 100) {
        nodes {
          author { login }
          body
          createdAt
          reactionGroups { reactors(first: 100) { nodes { ... on Actor { login } } } }
        }
      }
      readyForReview: timelineItems(last: 1, itemTypes: [READY_FOR_REVIEW_EVENT]) {
        nodes { ... on ReadyForReviewEvent { createdAt } }
      }
      reviewThreads(first: 100) {
        nodes {
          isResolved
          participants: comments(first: 100) { nodes { author { login } } }
          latest: comments(last: 1) {
            nodes {
              author { login }
              createdAt
              reactionGroups { reactors(first: 100) { nodes { ... on Actor { login } } } }
            }
          }
        }
      }
    }
  }
}
"""
# pylint: enable=duplicate-code


@dataclass(frozen=True)
class Review:
    """One submitted review; replies in threads also create one on GitHub."""

    author: str
    state: str
    body: str
    submitted_at: str
    commit_oid: str
    reply_only: bool
    reactors: frozenset[str]


@dataclass(frozen=True)
class Comment:
    """One conversation comment on the pull request."""

    author: str
    body: str
    created_at: str
    reactors: frozenset[str]


@dataclass(frozen=True)
class ReviewThread:
    resolved: bool
    participants: frozenset[str]
    last_author: str
    last_created_at: str
    last_reactors: frozenset[str]


@dataclass(frozen=True)
class PullRequest:
    """One pull request as the patrol sees it; logins are normalized."""

    node_id: str
    number: int
    url: str
    title: str
    body: str
    state: str
    is_draft: bool
    created_at: str
    repository: str
    author: str
    head_oid: str
    ready_at: str
    requested_reviewers: frozenset[str]
    reviews: tuple[Review, ...]
    comments: tuple[Comment, ...]
    threads: tuple[ReviewThread, ...]


def _login(node: object) -> str:
    if not isinstance(node, dict):
        return ""
    return normalize_login(str(node.get("login") or ""))


def _nodes(node: object, key: str) -> list[dict]:
    if not isinstance(node, dict):
        return []
    connection = node.get(key) or {}
    return [item for item in connection.get("nodes") or [] if isinstance(item, dict)]


def _reactors(node: dict) -> frozenset[str]:
    """Who reacted, bots included: ``Reaction.user`` names only users."""
    return frozenset(
        _login(reactor)
        for group in node.get("reactionGroups") or []
        if isinstance(group, dict)
        for reactor in _nodes(group, "reactors")
    )


def parse_pull_request(node: dict, repository: str) -> PullRequest:
    """Build a :class:`PullRequest` from a ``PULL_REQUEST_QUERY`` node."""
    reviews = []
    for review in _nodes(node, "reviews"):
        replies = _nodes(review, "comments")
        reviews.append(
            Review(
                author=_login(review.get("author")),
                state=str(review.get("state") or ""),
                body=str(review.get("body") or ""),
                submitted_at=str(review.get("submittedAt") or ""),
                commit_oid=str((review.get("commit") or {}).get("oid") or ""),
                reply_only=bool(replies)
                and all(reply.get("replyTo") for reply in replies),
                reactors=_reactors(review),
            )
        )
    threads = []
    for thread in _nodes(node, "reviewThreads"):
        latest = _nodes(thread, "latest")
        last = latest[-1] if latest else {}
        threads.append(
            ReviewThread(
                resolved=bool(thread.get("isResolved")),
                participants=frozenset(
                    _login(comment.get("author"))
                    for comment in _nodes(thread, "participants")
                ),
                last_author=_login(last.get("author")),
                last_created_at=str(last.get("createdAt") or ""),
                last_reactors=_reactors(last),
            )
        )
    comments = sorted(
        (
            Comment(
                author=_login(comment.get("author")),
                body=str(comment.get("body") or ""),
                created_at=str(comment.get("createdAt") or ""),
                reactors=_reactors(comment),
            )
            for comment in _nodes(node, "comments")
        ),
        key=lambda comment: comment.created_at,
    )
    return PullRequest(
        node_id=str(node.get("id") or ""),
        number=int(node.get("number") or 0),
        url=str(node.get("url") or ""),
        title=str(node.get("title") or ""),
        body=str(node.get("body") or ""),
        state=str(node.get("state") or "OPEN"),
        is_draft=bool(node.get("isDraft")),
        created_at=str(node.get("createdAt") or ""),
        repository=repository,
        author=_login(node.get("author")),
        head_oid=str(node.get("headRefOid") or ""),
        ready_at=max(
            (
                str(event.get("createdAt") or "")
                for event in _nodes(node, "readyForReview")
            ),
            default="",
        ),
        requested_reviewers=frozenset(
            _login(request.get("requestedReviewer"))
            for request in _nodes(node, "reviewRequests")
        )
        - {""},
        reviews=tuple(sorted(reviews, key=lambda review: review.submitted_at)),
        comments=tuple(comments),
        threads=tuple(threads),
    )


def _is_notice(comment: Comment) -> bool:
    return parse_workflow_status_comment(comment.body) is not None


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

    GitHub records a thread reply as a review, so the reviews cover it.
    """
    return max(
        [
            comment.created_at
            for comment in pr.comments
            if comment.author == me and not _is_notice(comment)
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
        if comment.author != me and not _is_notice(comment)
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
            if comment.author != me and not _is_notice(comment)
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
    status = parse_workflow_status_comment(latest.body)
    if status is None or not suppresses_ticket_selection(status):
        return False
    lifted_at = max(_latest_activity_by_others(pr, me), pr.ready_at)
    return lifted_at <= latest.created_at


def _reviewed_heads(pr: PullRequest, me: str, since: str = "") -> set[str]:
    """Head commits the member reviewed after *since*, excluding reply-only
    submissions.

    A thread reply is recorded by GitHub as a review at the current head, so
    counting it would both inflate the rounds and hide the commits it landed on
    from the re-review check.
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

    That is a human marking it ready for review, read from GitHub rather than
    inferred from a notice: a notice can land before or after another
    member's run fails, while the human's decision comes after both. A
    review-limit notice by any member also counts, for PRs announced before
    the limit made them a draft.
    """
    return max(
        [pr.ready_at]
        + [
            comment.created_at
            for comment in pr.comments
            if (status := parse_workflow_status_comment(comment.body)) is not None
            and status.reason == REVIEW_LIMIT_REASON
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
        me: The member's GitHub login, passed through ``normalize_login``.

    Returns:
        ``FEEDBACK`` or ``REVIEW`` for work to dispatch, ``REVIEW_LIMIT`` when
        the re-review budget is exhausted, else ``None``.
    """
    if pr.state != "OPEN" or pr.is_draft or _is_suppressed(pr, me):
        return None
    if pr.author == me:
        return (
            FEEDBACK
            if _has_unanswered_thread(pr, me, mine_only=False)
            or _has_unanswered_statement(pr, me)
            else None
        )
    return _review_work(pr, me)
