"""One pull request as GitHub's patrol reads it: the GraphQL document of
its snapshot, and its parsing into the patrol's
:class:`~guildbotics.integrations.pull_request_patrol.PullRequest`, whose
decision lives in that module.

The project board only knows issues, so pull requests reach a member through
the roles GitHub records on the PR itself. Fetching and dispatching live in
:class:`~guildbotics.integrations.github.code_hosting_service.GitHubCodeHostingService`.
"""

from __future__ import annotations

from guildbotics.integrations.github.github_utils import normalize_login
from guildbotics.integrations.github.workflow_status_comment import (
    parse_workflow_status_comment,
)
from guildbotics.integrations.pull_request_patrol import (
    Comment,
    PullRequest,
    Review,
    ReviewThread,
)

PULL_REQUEST_FEEDBACK_SOURCE_QUERIES = {
    "conversation_comments": "comments(last: 100)",
    "review_summaries": "reviews(last: 100)",
    "review_threads": "reviewThreads(first: 100)",
}
PULL_REQUEST_FEEDBACK_SOURCES = frozenset(PULL_REQUEST_FEEDBACK_SOURCE_QUERIES)

# A query names the fields it reads, and a pull request is identified by the
# same ones an issue is (the ticket service's project query reads them too).
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


def parse_pull_request(node: dict) -> PullRequest:
    """Build a :class:`PullRequest` from a ``PULL_REQUEST_QUERY`` node."""
    reviews = []
    for review in _nodes(node, "reviews"):
        replies = _nodes(review, "comments")
        reviews.append(
            Review(
                author=_login(review.get("author")),
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
                created_at=str(comment.get("createdAt") or ""),
                reactors=_reactors(comment),
                status=parse_workflow_status_comment(str(comment.get("body") or "")),
            )
            for comment in _nodes(node, "comments")
        ),
        key=lambda comment: comment.created_at,
    )
    return PullRequest(
        id=str(node.get("id") or ""),
        label=f"#{node.get('number') or 0}",
        url=str(node.get("url") or ""),
        title=str(node.get("title") or ""),
        body=str(node.get("body") or ""),
        open=str(node.get("state") or "OPEN") == "OPEN",
        is_draft=bool(node.get("isDraft")),
        created_at=str(node.get("createdAt") or ""),
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
