"""The pull request patrol, as the GitHub code host serves it: the PRs that
name the member, what each asks of it, and the hand-over at the re-review
limit."""

from typing import Any

import pytest

from guildbotics.entities.task import Task
from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.github.code_hosting_service import (
    GitHubCodeHostingService,
)
from guildbotics.integrations.github.repository_scope import (
    CONVERT_PULL_REQUEST_TO_DRAFT,
)
from guildbotics.integrations.github.workflow_status_comment import (
    parse_workflow_status_comment,
    render_workflow_status_comment,
)
from guildbotics.utils.i18n_tool import t


class _Response:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class _CodeHost(GitHubCodeHostingService):
    """GitHub's answers to the patrol: search hits and the GraphQL node per PR."""

    def __init__(self) -> None:
        person = Person(
            person_id="aiko", name="Aiko", account_info={"github_username": "aiko-gh"}
        )
        project = Project(
            name="demo",
            services={
                "code_hosting_service": {"name": "github", "owner": "GuildBotics"}
            },
        )
        super().__init__(person, Team(project=project, members=[person]))
        self.search_items: list[dict[str, Any]] = []
        self.pull_request_nodes: dict[int, dict[str, Any]] = {}
        #: The comments posted, as GitHub would show them.
        self.comments_added: list[tuple[str, str]] = []
        self.graphql_queries: list[str] = []
        #: What GitHub answers a draft conversion with, when it fails.
        self.draft_error: Exception | None = None

    async def _search_pull_requests(self) -> list[dict[str, Any]]:
        return self.search_items

    async def comment(self, url, body, *, kind=None, status=None):
        assert status is not None
        self.comments_added.append(
            (url, render_workflow_status_comment(body=body, payload=status))
        )

    async def _graphql(self, query: str, variables: dict) -> dict:
        self.graphql_queries.append(query)
        if query == CONVERT_PULL_REQUEST_TO_DRAFT:
            if self.draft_error is not None:
                raise self.draft_error
            for node in self.pull_request_nodes.values():
                if node["id"] == variables["pullRequest"]:
                    node["isDraft"] = True
            return {"convertPullRequestToDraft": {"pullRequest": {"isDraft": True}}}
        node = self.pull_request_nodes.get(int(variables.get("number") or 0))
        return {"repository": {"pullRequest": node}}

    async def first_task(self) -> Task | None:
        candidates = await self.pull_request_candidates()
        return candidates[0] if candidates else None


def _patrol_manager(node: dict[str, Any]) -> _CodeHost:
    manager = _CodeHost()
    manager.search_items = [_search_item(node["number"])]
    manager.pull_request_nodes = {node["number"]: node}
    return manager


def _search_item(number: int = 2, repo: str = "repo") -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/GuildBotics/{repo}/pull/{number}",
        "repository_url": f"https://api.github.com/repos/GuildBotics/{repo}",
        "updated_at": f"2026-01-0{number}T00:00:00Z",
    }


def _pull_request_node(
    *,
    number: int = 2,
    author: str = "aiko-gh",
    head: str = "head-2",
    reviews: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
    threads: list[dict[str, Any]] | None = None,
    requested: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"PR{number}",
        "number": number,
        "url": f"https://github.com/GuildBotics/repo/pull/{number}",
        "title": f"pr {number}",
        "body": "body",
        "createdAt": "2026-01-01T00:00:00Z",
        "headRefOid": head,
        "author": {"login": author},
        "reviewRequests": {
            "nodes": [
                {"requestedReviewer": {"login": login}} for login in requested or []
            ]
        },
        "reviews": {"nodes": reviews or []},
        "comments": {"nodes": comments or []},
        "reviewThreads": {"nodes": threads or []},
    }


def _review(
    author: str,
    *,
    commit: str,
    submitted_at: str = "2026-01-01T01:00:00Z",
    state: str = "COMMENTED",
    body: str = "",
    reply: bool = False,
) -> dict[str, Any]:
    return {
        "author": {"login": author},
        "state": state,
        "body": body,
        "submittedAt": submitted_at,
        "commit": {"oid": commit},
        "comments": {"nodes": [{"replyTo": {"id": "root"} if reply else None}]},
    }


def _thread(last_author: str, *participants: str, resolved: bool = False) -> dict:
    return {
        "isResolved": resolved,
        "participants": {
            "nodes": [
                {"author": {"login": login}} for login in (*participants, last_author)
            ]
        },
        "latest": {"nodes": [{"author": {"login": last_author}, "reactionGroups": []}]},
    }


def _past_the_review_limit() -> dict[str, Any]:
    """Someone else's PR with three rounds behind the member and a new head."""
    return _pull_request_node(
        author="other",
        head="head-4",
        reviews=[
            _review(
                "aiko-gh",
                commit=f"head-{index}",
                submitted_at=f"2026-01-0{index}T00:00:00Z",
            )
            for index in (1, 2, 3)
        ],
    )


def _posted(manager: _CodeHost, index: int, at: str) -> dict[str, Any]:
    """The member's comment ``index`` as the next snapshot shows it."""
    return {
        "author": {"login": "aiko-gh"},
        "body": manager.comments_added[index][1],
        "createdAt": at,
    }


class _SearchClient:
    """Records search calls and answers each qualifier with its own hits."""

    def __init__(self, hits: dict[str, list[dict[str, Any]]], status: int = 200):
        self.hits = hits
        self.status = status
        self.calls: list[dict[str, Any]] = []

    async def get(self, endpoint: str, **kwargs):
        assert endpoint == "/search/issues"
        params = kwargs["params"]
        self.calls.append(params)
        qualifier = params["q"].split()[-1].split(":")[0]
        response = _Response({"items": self.hits.get(qualifier, [])}, self.status)
        response.text = "boom"
        return response


@pytest.mark.asyncio
async def test_own_pr_with_unanswered_review_thread_is_feedback_work():
    manager = _patrol_manager(_pull_request_node(threads=[_thread("reviewer")]))

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "pull_request_feedback"
    assert task.pull_request_url == "https://github.com/GuildBotics/repo/pull/2"
    assert task.url == task.pull_request_url
    assert task.number == 2
    assert task.repository == "repo"
    assert task.status == Task.IN_PROGRESS
    assert task.assignee == "aiko"


@pytest.mark.asyncio
async def test_refresh_pull_request_skips_closed_or_draft_candidate():
    node = _pull_request_node(threads=[_thread("reviewer")])
    manager = _patrol_manager(node)
    candidate = (await manager.pull_request_candidates())[0]

    node["state"] = "CLOSED"
    assert await manager.refresh_pull_request(candidate) is None

    node["state"] = "OPEN"
    node["isDraft"] = True
    assert await manager.refresh_pull_request(candidate) is None


@pytest.mark.asyncio
async def test_reviewed_pr_with_new_commits_is_review_work():
    manager = _patrol_manager(
        _pull_request_node(
            author="other", head="head-3", reviews=[_review("aiko-gh", commit="head-2")]
        )
    )

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "pull_request_review"


@pytest.mark.asyncio
async def test_review_limit_converts_the_pr_to_a_draft_then_announces_it():
    node = _past_the_review_limit()
    manager = _patrol_manager(node)
    drafts_when_commented: list[int] = []
    comment = manager.comment

    async def add_comment(url, body, **kwargs):
        drafts_when_commented.append(
            manager.graphql_queries.count(CONVERT_PULL_REQUEST_TO_DRAFT)
        )
        await comment(url, body, **kwargs)

    manager.comment = add_comment  # type: ignore[method-assign]

    assert await manager.first_task() is None
    assert node["isDraft"] is True
    assert drafts_when_commented == [1]
    [(url, body)] = manager.comments_added
    assert url == "https://github.com/GuildBotics/repo/pull/2"
    status = parse_workflow_status_comment(body)
    assert status is not None and status.reason == "review_limit"
    assert (
        t("integrations.github.github_ticket_manager.review_limit_reached", count=3)
        in body
    )

    # The draft is the human's: neither patrol acts on it.
    node["comments"]["nodes"].append(_posted(manager, 0, "2026-01-09T00:00:00Z"))
    assert await manager.first_task() is None
    assert len(manager.comments_added) == 1

    # Ready for review again: the rounds start over from that point.
    node["isDraft"] = False
    node["readyForReview"] = {"nodes": [{"createdAt": "2026-01-10T00:00:00Z"}]}
    resumed = await manager.first_task()
    assert resumed is not None
    assert resumed.trigger_reason == "pull_request_review"
    assert manager.graphql_queries.count(CONVERT_PULL_REQUEST_TO_DRAFT) == 1


@pytest.mark.asyncio
async def test_a_failed_draft_conversion_holds_the_pr_without_the_limit_notice():
    node = _past_the_review_limit()
    manager = _patrol_manager(node)
    manager.draft_error = RuntimeError("Resource not accessible by integration")

    assert await manager.first_task() is None
    assert not node.get("isDraft")
    [(_, body)] = manager.comments_added
    status = parse_workflow_status_comment(body)
    assert status is not None and status.reason == "failed"
    assert (
        t(
            "integrations.github.github_ticket_manager.review_limit_draft_failed",
            count=3,
        )
        in body
    )

    # The failure notice holds the PR until someone else acts on it.
    node["comments"]["nodes"].append(_posted(manager, 0, "2026-01-09T00:00:00Z"))
    assert await manager.first_task() is None
    assert len(manager.comments_added) == 1

    # Then the limit still stands, and the conversion is tried again.
    node["comments"]["nodes"].append(
        {
            "author": {"login": "other"},
            "body": "Fixed",
            "createdAt": "2026-01-10T00:00:00Z",
        }
    )
    manager.draft_error = None
    assert await manager.first_task() is None
    assert node["isDraft"] is True
    status = parse_workflow_status_comment(manager.comments_added[1][1])
    assert status is not None and status.reason == "review_limit"


@pytest.mark.asyncio
async def test_marking_ready_after_a_failed_conversion_resumes_the_review():
    """A human may make the PR a draft by hand and then ready again; that
    lifts the failure hold and starts the rounds over."""
    node = _past_the_review_limit()
    manager = _patrol_manager(node)
    manager.draft_error = RuntimeError("Resource not accessible by integration")
    assert await manager.first_task() is None
    node["comments"]["nodes"].append(_posted(manager, 0, "2026-01-09T00:00:00Z"))

    node["readyForReview"] = {"nodes": [{"createdAt": "2026-01-10T00:00:00Z"}]}
    resumed = await manager.first_task()

    assert resumed is not None
    assert resumed.trigger_reason == "pull_request_review"
    assert len(manager.comments_added) == 1


@pytest.mark.asyncio
async def test_oldest_updated_pull_request_is_served_first():
    manager = _CodeHost()
    manager.search_items = [_search_item(3), _search_item(2)]
    manager.pull_request_nodes = {
        2: _pull_request_node(number=2, threads=[_thread("reviewer")]),
        3: _pull_request_node(number=3, threads=[_thread("reviewer")]),
    }

    task = await manager.first_task()

    assert task is not None and task.number == 3


@pytest.mark.asyncio
async def test_search_covers_both_roles_and_dedupes_oldest_first():
    manager = _CodeHost()
    client = _SearchClient(
        {
            "author": [_search_item(3), _search_item(2)],
            "reviewed-by": [_search_item(2), _search_item(4)],
            "review-requested": [_search_item(1)],
        }
    )
    manager._client = client  # type: ignore[assignment]

    items = await GitHubCodeHostingService._search_pull_requests(manager)

    # ``draft:false`` keeps a PR a human converted to draft out of both roles.
    assert [params["q"] for params in client.calls] == [
        "is:pr is:open draft:false user:GuildBotics author:aiko-gh",
        "is:pr is:open draft:false user:GuildBotics reviewed-by:aiko-gh",
        "is:pr is:open draft:false user:GuildBotics review-requested:aiko-gh",
    ]
    assert all(params["per_page"] == 100 for params in client.calls)
    assert [item["number"] for item in items] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_search_failure_is_not_silently_downgraded():
    manager = _CodeHost()
    manager._client = _SearchClient({}, status=422)  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="422"):
        await GitHubCodeHostingService._search_pull_requests(manager)
