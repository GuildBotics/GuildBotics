"""Decision tests for the pull request patrol.

Each case is one snapshot of a PR and the answer to "what does it ask of
me?". Fetching is not involved; see the ticket manager tests for that.
"""

from __future__ import annotations

from typing import Any

import pytest

from guildbotics.capabilities.member_github import PR_INSPECT_FEEDBACK_SOURCES
from guildbotics.integrations.chat_workflow_status import workflow_status_fields
from guildbotics.integrations.github.github_utils import normalize_login
from guildbotics.integrations.github.pull_request_patrol import (
    FEEDBACK,
    MAX_REVIEW_ROUNDS,
    PULL_REQUEST_FEEDBACK_SOURCE_QUERIES,
    PULL_REQUEST_FEEDBACK_SOURCES,
    PULL_REQUEST_QUERY,
    REVIEW,
    REVIEW_LIMIT,
    PullRequest,
    parse_pull_request,
    pull_request_work,
    review_rounds,
)
from guildbotics.integrations.workflow_status_comment import (
    render_workflow_status_comment,
)

ME = "aiko-gh"


def _node(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": "PR1",
        "number": 1,
        "url": "https://github.com/GuildBotics/repo/pull/1",
        "title": "Title",
        "body": "Body",
        "createdAt": "2026-01-01T00:00:00Z",
        "headRefOid": "head-1",
        "author": {"login": "Aiko-GH"},
        "reviewRequests": {"nodes": []},
        "reviews": {"nodes": []},
        "comments": {"nodes": []},
        "reviewThreads": {"nodes": []},
    }
    node.update(overrides)
    return node


def _review(
    author: str,
    *,
    commit: str = "head-1",
    at: str = "2026-01-02T00:00:00Z",
    state: str = "COMMENTED",
    body: str = "",
    replies: list[bool] | None = None,
) -> dict[str, Any]:
    comments = [
        {"replyTo": {"id": "root"} if reply else None} for reply in (replies or [])
    ]
    return {
        "author": {"login": author},
        "state": state,
        "body": body,
        "submittedAt": at,
        "commit": {"oid": commit},
        "comments": {"nodes": comments},
    }


def _comment(author: str, body: str, at: str) -> dict[str, Any]:
    return {"author": {"login": author}, "body": body, "createdAt": at}


def _thread(
    *authors: str,
    resolved: bool = False,
    reactors: list[str] | None = None,
    at: str = "2026-01-02T00:00:00Z",
) -> dict[str, Any]:
    return {
        "isResolved": resolved,
        "participants": {"nodes": [{"author": {"login": a}} for a in authors]},
        "latest": {
            "nodes": [
                {
                    "author": {"login": authors[-1]},
                    "createdAt": at,
                    "reactions": {
                        "nodes": [{"user": {"login": r}} for r in reactors or []]
                    },
                }
            ]
        },
    }


def _notice(reason: str, **payload: str) -> str:
    return render_workflow_status_comment(
        body="notice",
        payload=workflow_status_fields(
            reason=reason, person_id="aiko", run_id="r", **payload
        ),
    )


def _work(node: dict[str, Any]) -> str | None:
    return pull_request_work(parse_pull_request(node, "repo"), ME)


def test_parse_lower_cases_logins_and_orders_by_time():
    pr = parse_pull_request(
        _node(
            reviewRequests={"nodes": [{"requestedReviewer": {"login": "Bob"}}, {}]},
            reviews={
                "nodes": [
                    _review("Bob", at="2026-01-03T00:00:00Z", replies=[True]),
                    _review("bob", at="2026-01-02T00:00:00Z", replies=[True, False]),
                ]
            },
            comments={
                "nodes": [
                    _comment("Bob", "later", "2026-01-05T00:00:00Z"),
                    _comment("bob", "earlier", "2026-01-04T00:00:00Z"),
                ]
            },
            reviewThreads={"nodes": [_thread("Bob", "Aiko-GH", reactors=["Bob"])]},
        ),
        "repo",
    )

    assert isinstance(pr, PullRequest)
    assert pr.author == ME
    assert pr.requested_reviewers == {"bob"}
    assert [review.submitted_at for review in pr.reviews] == [
        "2026-01-02T00:00:00Z",
        "2026-01-03T00:00:00Z",
    ]
    assert [review.reply_only for review in pr.reviews] == [False, True]
    assert [comment.body for comment in pr.comments] == ["earlier", "later"]
    thread = pr.threads[0]
    assert thread.participants == {"bob", ME}
    assert thread.last_author == ME
    assert thread.last_created_at == "2026-01-02T00:00:00Z"
    assert thread.last_reactors == {"bob"}


def test_graphql_bot_logins_match_the_members_bot_username():
    """GraphQL names a GitHub App ``<app>``; the member is ``<app>[bot]``."""
    app = normalize_login("aiko-guildbotics-com[bot]")
    node = _node(
        author={"login": "other"},
        headRefOid="head-2",
        reviewThreads={"nodes": [_thread("aiko-guildbotics-com", "other")]},
    )

    assert pull_request_work(parse_pull_request(node, "repo"), app) == REVIEW

    own = _node(
        author={"login": "aiko-guildbotics-com"},
        reviewThreads={"nodes": [_thread("reviewer", "aiko-guildbotics-com")]},
    )
    assert pull_request_work(parse_pull_request(own, "repo"), app) is None


def test_query_asks_for_everything_the_decision_reads():
    for field in (
        "headRefOid",
        "reviewRequests",
        "replyTo",
        "participants: comments",
        "latest: comments(last: 1)",
        "reactions(first: 100)",
        *PULL_REQUEST_FEEDBACK_SOURCE_QUERIES.values(),
    ):
        assert field in PULL_REQUEST_QUERY, field


def test_patrol_and_pr_inspect_expose_the_same_feedback_sources():
    assert PULL_REQUEST_FEEDBACK_SOURCES == PR_INSPECT_FEEDBACK_SOURCES


# --- author role --------------------------------------------------------- #


def test_own_pr_without_activity_asks_nothing():
    assert _work(_node()) is None


def test_own_pr_with_thread_answered_by_someone_else_is_feedback():
    assert _work(_node(reviewThreads={"nodes": [_thread("reviewer")]})) == FEEDBACK


@pytest.mark.parametrize(
    "thread",
    [
        _thread("reviewer", resolved=True),
        _thread("reviewer", ME),
        _thread("reviewer", reactors=[ME]),
    ],
    ids=["resolved", "my_reply_last", "my_reaction"],
)
def test_own_pr_thread_is_answered(thread):
    assert _work(_node(reviewThreads={"nodes": [thread]})) is None


def test_own_pr_with_new_conversation_comment_is_feedback():
    node = _node(
        comments={"nodes": [_comment("human", "Please also", "2026-01-03T00:00:00Z")]}
    )

    assert _work(node) == FEEDBACK


def test_own_pr_comment_answered_by_my_later_reply_asks_nothing():
    node = _node(
        comments={
            "nodes": [
                _comment("human", "Please also", "2026-01-03T00:00:00Z"),
                _comment(ME, "Done", "2026-01-04T00:00:00Z"),
            ]
        }
    )

    assert _work(node) is None


def test_own_pr_review_summary_is_feedback_until_i_reply():
    review = _review(
        "reviewer", state="CHANGES_REQUESTED", body="Fix", at="2026-01-03T00:00:00Z"
    )

    assert _work(_node(reviews={"nodes": [review]})) == FEEDBACK
    answered = _node(
        reviews={"nodes": [review, _review(ME, at="2026-01-04T00:00:00Z")]}
    )
    assert _work(answered) is None


@pytest.mark.parametrize(
    "review",
    [
        _review("reviewer", state="APPROVED", body="LGTM"),
        _review("reviewer", state="COMMENTED", body="   "),
    ],
    ids=["approval", "empty_body"],
)
def test_own_pr_review_without_a_request_is_not_feedback(review):
    assert _work(_node(reviews={"nodes": [review]})) is None


def test_own_pr_status_notices_are_neither_feedback_nor_replies():
    node = _node(
        comments={
            "nodes": [
                _comment("human", "Please fix", "2026-01-03T00:00:00Z"),
                _comment(
                    ME,
                    _notice("rate_limited", retry_after_at="2026-01-04T01:00:00+00:00"),
                    "2026-01-04T00:00:00Z",
                ),
            ]
        }
    )
    # An expired rate limit no longer suppresses, and my notice did not
    # answer the human, so the request is still pending.
    assert _work(node) == FEEDBACK

    reviewer_notice = _node(
        comments={
            "nodes": [_comment("bob", _notice("review_limit"), "2026-01-03T00:00:00Z")]
        }
    )
    assert _work(reviewer_notice) is None


def test_failed_notice_as_my_latest_comment_suppresses_earlier_feedback():
    node = _node(
        reviewThreads={"nodes": [_thread("reviewer", at="2026-01-02T00:00:00Z")]},
        comments={"nodes": [_comment(ME, _notice("failed"), "2026-01-03T00:00:00Z")]},
    )

    assert _work(node) is None


@pytest.mark.parametrize(
    "later_activity",
    [
        {"reviewThreads": {"nodes": [_thread("reviewer", at="2026-01-04T00:00:00Z")]}},
        {
            "reviews": {
                "nodes": [
                    _review(
                        "reviewer",
                        state="CHANGES_REQUESTED",
                        body="Fix",
                        at="2026-01-04T00:00:00Z",
                    )
                ]
            }
        },
    ],
    ids=["thread_reply", "review"],
)
def test_activity_after_a_failure_notice_lifts_the_suppression(later_activity):
    """A failure puts the PR on hold until someone acts on it, in any place."""
    node = _node(
        comments={"nodes": [_comment(ME, _notice("failed"), "2026-01-03T00:00:00Z")]},
        **later_activity,
    )

    assert _work(node) == FEEDBACK


def test_thread_reply_after_a_failure_notice_restarts_review_work():
    node = _theirs(
        reviewThreads={"nodes": [_thread(ME, "other", at="2026-01-04T00:00:00Z")]},
        comments={"nodes": [_comment(ME, _notice("failed"), "2026-01-03T00:00:00Z")]},
    )

    assert _work(node) == REVIEW


# --- reviewer role ------------------------------------------------------- #


def _theirs(**overrides: Any) -> dict[str, Any]:
    return _node(author={"login": "other"}, headRefOid="head-2", **overrides)


def test_someone_elses_pr_i_never_touched_asks_nothing():
    assert _work(_theirs(reviewThreads={"nodes": [_thread("other")]})) is None


def test_requested_review_is_review_work():
    node = _theirs(reviewRequests={"nodes": [{"requestedReviewer": {"login": ME}}]})

    assert _work(node) == REVIEW


def test_submitted_review_consumes_the_request_and_settles_the_pr():
    """GitHub drops the request once the member submits a review (``pr review``);
    the snapshot then shows only that review at the current head."""
    node = _theirs(reviews={"nodes": [_review(ME, commit="head-2", body="LGTM")]})

    assert _work(node) is None


def test_reply_in_my_thread_is_review_work():
    assert _work(_theirs(reviewThreads={"nodes": [_thread(ME, "other")]})) == REVIEW
    assert _work(_theirs(reviewThreads={"nodes": [_thread("other", ME)]})) is None


def test_new_commits_after_my_review_are_review_work():
    reviewed = _theirs(reviews={"nodes": [_review(ME, commit="head-2")]})
    moved = _theirs(reviews={"nodes": [_review(ME, commit="head-1")]})

    assert _work(reviewed) is None
    assert _work(moved) == REVIEW


def test_thread_replies_do_not_count_as_reviewing_the_new_head():
    node = _theirs(
        reviews={
            "nodes": [
                _review(ME, commit="head-1", replies=[False]),
                _review(ME, commit="head-2", at="2026-01-03T00:00:00Z", replies=[True]),
            ]
        }
    )

    assert review_rounds(parse_pull_request(node, "repo"), ME) == {"head-1"}
    assert _work(node) == REVIEW


def _rounds(count: int) -> list[dict[str, Any]]:
    return [
        _review(ME, commit=f"round-{index}", at=f"2026-01-0{index}T00:00:00Z")
        for index in range(1, count + 1)
    ]


def test_re_review_stops_at_the_round_limit():
    assert MAX_REVIEW_ROUNDS == 3
    assert _work(_theirs(reviews={"nodes": _rounds(2)})) == REVIEW
    assert _work(_theirs(reviews={"nodes": _rounds(3)})) == REVIEW_LIMIT


def test_review_limit_is_announced_only_once():
    announced = _theirs(
        reviews={"nodes": _rounds(3)},
        comments={
            "nodes": [_comment(ME, _notice("review_limit"), "2026-01-05T00:00:00Z")]
        },
    )

    assert _work(announced) is None


def test_only_explicit_request_outlives_the_limit():
    requested = _theirs(
        reviews={"nodes": _rounds(3)},
        reviewRequests={"nodes": [{"requestedReviewer": {"login": ME}}]},
    )
    replied = _theirs(
        reviews={"nodes": _rounds(3)},
        reviewThreads={"nodes": [_thread(ME, "other")]},
    )

    assert _work(requested) == REVIEW
    assert _work(replied) == REVIEW_LIMIT


def test_thread_reply_after_the_review_limit_notice_asks_nothing():
    node = _theirs(
        reviews={"nodes": _rounds(3)},
        comments={
            "nodes": [_comment(ME, _notice("review_limit"), "2026-01-05T00:00:00Z")]
        },
        reviewThreads={"nodes": [_thread(ME, "other", at="2026-01-06T00:00:00Z")]},
    )

    assert _work(node) is None


def test_thread_reply_before_the_review_limit_is_review_work():
    node = _theirs(
        reviews={
            "nodes": [
                _review(ME, commit="round-1"),
                _review(ME, commit="head-2"),
            ]
        },
        reviewThreads={"nodes": [_thread(ME, "other")]},
    )

    assert _work(node) == REVIEW
