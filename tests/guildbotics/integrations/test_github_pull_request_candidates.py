"""The pull request patrol, as the GitHub code host serves it: the PRs that
name the member, what each asks of it, and the hand-over at the re-review
limit."""

import json
from typing import Any

import httpx
import pytest

from guildbotics.entities.task import Task
from guildbotics.integrations.github.code_hosting_service import (
    GitHubCodeHostingService,
)
from guildbotics.integrations.github.pull_request_patrol import PULL_REQUEST_QUERY
from guildbotics.integrations.github.repository_scope import (
    CONVERT_PULL_REQUEST_TO_DRAFT,
    NODE_REPOSITORY,
)
from guildbotics.integrations.github.workflow_status_comment import (
    parse_workflow_status_comment,
)
from guildbotics.runtime.port import ServiceError, ServiceNotFound
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.integrations.github_services import (
    LOGIN,
    OWNER,
    REPO,
    ScriptedGitHub,
    code_host,
    pull_url,
)


class _PatrolGitHub(ScriptedGitHub):
    """GitHub as the patrol meets it: search hits, a GraphQL node per PR, the
    draft conversion, and the comments the member posts."""

    def __init__(self) -> None:
        super().__init__()
        self.search_items: list[dict[str, Any]] = []
        self.pull_request_nodes: dict[int, dict[str, Any]] = {}
        #: The comments posted, as GitHub shows them.
        self.comments_added: list[tuple[str, str]] = []
        #: Whether GitHub refuses the draft conversion.
        self.draft_refused = False
        self.on("GET", "/search/issues", {"items": []})
        self.on_graphql(
            NODE_REPOSITORY,
            {"node": {"repository": {"name": "demo", "owner": {"login": OWNER}}}},
        )
        self.on_graphql(CONVERT_PULL_REQUEST_TO_DRAFT, self._convert)
        self.on_graphql(PULL_REQUEST_QUERY, self._pull_request)

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/search/issues":
            self.rest[("GET", path)] = {"items": self.search_items}
        elif path.startswith(f"/repos/{REPO}/"):
            number = int(path.rsplit("/", 2)[1 if path.endswith("comments") else 2])
            node = self.pull_request_nodes[number]
            self.rest[("GET", f"/repos/{REPO}/pulls/{number}")] = {
                "number": number,
                "title": node["title"],
                "html_url": node["url"],
                "user": node["author"],
            }
            self.rest[("POST", f"/repos/{REPO}/issues/{number}/comments")] = lambda r: (
                self._comment(node["url"], r)
            )
        return super().respond(request)

    def _comment(self, url: str, request: httpx.Request) -> dict[str, Any]:
        self.comments_added.append((url, json.loads(request.content)["body"]))
        return {
            "id": len(self.comments_added),
            "html_url": f"{url}#issuecomment-{len(self.comments_added)}",
            "user": {"login": LOGIN},
            "created_at": "2026-01-09T00:00:00Z",
        }

    def _convert(self, variables: dict[str, Any]) -> Any:
        if self.draft_refused:
            return httpx.Response(200, json={"errors": [{"type": "FORBIDDEN"}]})
        for node in self.pull_request_nodes.values():
            if node["id"] == variables["pullRequest"]:
                node["isDraft"] = True
        return {"convertPullRequestToDraft": {"pullRequest": {"isDraft": True}}}

    def _pull_request(self, variables: dict[str, Any]) -> dict[str, Any]:
        assert (variables["owner"], variables["repo"]) == tuple(REPO.split("/"))
        return {
            "repository": {
                "pullRequest": self.pull_request_nodes.get(variables["number"])
            }
        }

    @property
    def conversions(self) -> int:
        return sum(
            body["query"] == CONVERT_PULL_REQUEST_TO_DRAFT for body in self.documents()
        )


async def _first_task(service: GitHubCodeHostingService) -> Task | None:
    candidates = await service.pull_request_candidates()
    return candidates[0] if candidates else None


def _patrol(monkeypatch, *nodes: dict[str, Any]):
    github = _PatrolGitHub()
    github.search_items = [_search_item(node["number"]) for node in nodes]
    github.pull_request_nodes = {node["number"]: node for node in nodes}
    return github, code_host(github, monkeypatch)


def _search_item(number: int = 2) -> dict[str, Any]:
    return {
        "number": number,
        "html_url": pull_url(number),
        "repository_url": f"https://api.github.com/repos/{REPO}",
        "updated_at": f"2026-01-0{number}T00:00:00Z",
    }


def _pull_request_node(
    *,
    number: int = 2,
    author: str = LOGIN,
    head: str = "head-2",
    reviews: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
    threads: list[dict[str, Any]] | None = None,
    requested: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"PR{number}",
        "number": number,
        "url": pull_url(number),
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
                LOGIN,
                commit=f"head-{index}",
                submitted_at=f"2026-01-0{index}T00:00:00Z",
            )
            for index in (1, 2, 3)
        ],
    )


def _posted(github: _PatrolGitHub, index: int, at: str) -> dict[str, Any]:
    """The member's comment ``index`` as the next snapshot shows it."""
    return {
        "author": {"login": LOGIN},
        "body": github.comments_added[index][1],
        "createdAt": at,
    }


@pytest.mark.asyncio
async def test_own_pr_with_unanswered_review_thread_is_feedback_work(monkeypatch):
    github, service = _patrol(monkeypatch, _pull_request_node(threads=[_thread("x")]))

    task = await _first_task(service)

    assert task is not None
    assert task.trigger_reason == "pull_request_feedback"
    assert task.id == "PR2"
    assert task.pull_request_url == pull_url(2)
    assert task.url == task.pull_request_url
    assert task.title == "pr 2"
    assert task.status == Task.IN_PROGRESS
    assert task.assignee == "aiko"
    assert task.trace_attributes()["target.kind"] == "pull_request"
    assert github.writes == []


@pytest.mark.asyncio
async def test_refresh_pull_request_skips_closed_or_draft_candidate(monkeypatch):
    node = _pull_request_node(threads=[_thread("reviewer")])
    _, service = _patrol(monkeypatch, node)
    candidate = (await service.pull_request_candidates())[0]

    assert await service.refresh_pull_request(candidate) == candidate
    node["state"] = "CLOSED"
    assert await service.refresh_pull_request(candidate) is None

    node["state"] = "OPEN"
    node["isDraft"] = True
    assert await service.refresh_pull_request(candidate) is None


@pytest.mark.asyncio
async def test_a_pull_request_github_no_longer_shows_is_not_found(monkeypatch):
    github, service = _patrol(monkeypatch, _pull_request_node())
    github.pull_request_nodes.clear()

    with pytest.raises(ServiceError, match="not there"):
        await service.refresh_pull_request(
            Task(
                id="PR2",
                url=pull_url(2),
                title="pr 2",
                description="",
                pull_request_url=pull_url(2),
            )
        )


@pytest.mark.asyncio
async def test_reviewed_pr_with_new_commits_is_review_work(monkeypatch):
    _, service = _patrol(
        monkeypatch,
        _pull_request_node(
            author="other", head="head-3", reviews=[_review(LOGIN, commit="head-2")]
        ),
    )

    task = await _first_task(service)

    assert task is not None
    assert task.trigger_reason == "pull_request_review"


@pytest.mark.asyncio
async def test_review_limit_converts_the_pr_to_a_draft_then_announces_it(monkeypatch):
    node = _past_the_review_limit()
    github, service = _patrol(monkeypatch, node)

    assert await _first_task(service) is None
    assert node["isDraft"] is True
    # The draft first, the notice after it.
    assert github.writes == [
        "POST /graphql",
        f"POST /repos/{REPO}/issues/2/comments",
    ]
    [(url, body)] = github.comments_added
    assert url == pull_url(2)
    assert body.startswith("@other\n\n")
    status = parse_workflow_status_comment(body)
    assert status is not None
    assert (status.reason, status.person_id, status.run_id, status.subject) == (
        "review_limit",
        "aiko",
        "",
        pull_url(2),
    )
    assert t("integrations.pull_request_patrol.review_limit_reached", count=3) in body

    # The draft is the human's: neither patrol acts on it.
    node["comments"]["nodes"].append(_posted(github, 0, "2026-01-09T00:00:00Z"))
    assert await _first_task(service) is None
    assert len(github.comments_added) == 1

    # Ready for review again: the rounds start over from that point.
    node["isDraft"] = False
    node["readyForReview"] = {"nodes": [{"createdAt": "2026-01-10T00:00:00Z"}]}
    resumed = await _first_task(service)
    assert resumed is not None
    assert resumed.trigger_reason == "pull_request_review"
    assert github.conversions == 1


@pytest.mark.asyncio
async def test_a_failed_draft_conversion_holds_the_pr_without_the_limit_notice(
    monkeypatch,
):
    node = _past_the_review_limit()
    github, service = _patrol(monkeypatch, node)
    github.draft_refused = True

    assert await _first_task(service) is None
    assert not node.get("isDraft")
    [(_, body)] = github.comments_added
    status = parse_workflow_status_comment(body)
    assert status is not None and status.reason == "failed"
    assert (
        t("integrations.pull_request_patrol.review_limit_draft_failed", count=3) in body
    )

    # The failure notice holds the PR until someone else acts on it.
    node["comments"]["nodes"].append(_posted(github, 0, "2026-01-09T00:00:00Z"))
    assert await _first_task(service) is None
    assert len(github.comments_added) == 1

    # Then the limit still stands, and the conversion is tried again.
    node["comments"]["nodes"].append(
        {
            "author": {"login": "other"},
            "body": "Fixed",
            "createdAt": "2026-01-10T00:00:00Z",
        }
    )
    github.draft_refused = False
    assert await _first_task(service) is None
    assert node["isDraft"] is True
    status = parse_workflow_status_comment(github.comments_added[1][1])
    assert status is not None and status.reason == "review_limit"


@pytest.mark.asyncio
async def test_marking_ready_after_a_failed_conversion_resumes_the_review(
    monkeypatch,
):
    """A human may make the PR a draft by hand and then ready again; that
    lifts the failure hold and starts the rounds over."""
    node = _past_the_review_limit()
    github, service = _patrol(monkeypatch, node)
    github.draft_refused = True
    assert await _first_task(service) is None
    node["comments"]["nodes"].append(_posted(github, 0, "2026-01-09T00:00:00Z"))

    node["readyForReview"] = {"nodes": [{"createdAt": "2026-01-10T00:00:00Z"}]}
    resumed = await _first_task(service)

    assert resumed is not None
    assert resumed.trigger_reason == "pull_request_review"
    assert len(github.comments_added) == 1


@pytest.mark.asyncio
async def test_oldest_updated_pull_request_is_served_first(monkeypatch):
    github, service = _patrol(
        monkeypatch,
        _pull_request_node(number=2, threads=[_thread("reviewer")]),
        _pull_request_node(number=3, threads=[_thread("reviewer")]),
    )
    github.search_items = [
        _search_item(2),
        {**_search_item(3), "updated_at": "2026-01-01T00:00:00Z"},
    ]

    task = await _first_task(service)

    assert task is not None and task.url == pull_url(3)


@pytest.mark.asyncio
async def test_search_covers_both_roles_and_dedupes_oldest_first(monkeypatch):
    hits = {
        "author": [_search_item(3), _search_item(2)],
        "reviewed-by": [_search_item(2), _search_item(4)],
        "review-requested": [_search_item(1)],
        "commenter": [_search_item(5)],
    }
    github = ScriptedGitHub().on(
        "GET",
        "/search/issues",
        lambda r: {"items": hits[r.url.params["q"].split()[-1].split(":")[0]]},
    )
    service = code_host(github, monkeypatch)

    items = await service._search_pull_requests()

    # ``draft:false`` keeps a PR a human converted to draft out of both roles.
    assert [r.url.params["q"] for r in github.requests] == [
        f"is:pr is:open draft:false user:{OWNER} author:{LOGIN}",
        f"is:pr is:open draft:false user:{OWNER} reviewed-by:{LOGIN}",
        f"is:pr is:open draft:false user:{OWNER} review-requested:{LOGIN}",
        f"is:pr is:open draft:false user:{OWNER} commenter:{LOGIN}",
    ]
    assert all(r.url.params["per_page"] == "100" for r in github.requests)
    assert [item["number"] for item in items] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_waiting_for_a_comment_participant_is_discovered_by_the_patrol(
    monkeypatch,
):
    github, service = _patrol(
        monkeypatch,
        _pull_request_node(
            author="other",
            comments=[
                {
                    "author": {"login": LOGIN},
                    "body": "A question",
                    "createdAt": "2026-01-01T00:00:00Z",
                },
                {
                    "author": {"login": "other"},
                    "body": "Please clarify",
                    "createdAt": "2026-01-02T00:00:00Z",
                },
            ],
        ),
    )
    # Search returns this PR only through its comment participant, not a review.
    github.respond = ScriptedGitHub.respond.__get__(github)
    github.on(
        "GET",
        "/search/issues",
        lambda request: {
            "items": [_search_item(2)]
            if "commenter:" in request.url.params["q"]
            else []
        },
    )
    assert await service.can_receive_work(pull_url(2))
    [task] = await service.pull_request_candidates()
    assert task.pull_request_url == pull_url(2)
    assert task.trigger_reason == "pull_request_review"
    assert github.conversions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("other_count", [0, 100])
async def test_waiting_recipient_must_be_in_the_patrols_actual_discovery(
    monkeypatch, other_count
):
    target = _pull_request_node(number=102, author="other", requested=[LOGIN])
    github, service = _patrol(monkeypatch, target)
    # The snapshot grants a requested-review role, but the actual search may
    # omit it through its host filters or the patrol's first-page limit.
    github.search_items = [_search_item(number) for number in range(1, other_count + 1)]
    github.pull_request_nodes.update(
        {
            number: _pull_request_node(number=number)
            for number in range(1, other_count + 1)
        }
    )
    assert await service._pull_request_work(pull_url(102)) is not None
    assert not await service.can_receive_work(pull_url(102))
    assert await service.pull_request_candidates() == []
    assert github.conversions == 0


@pytest.mark.asyncio
async def test_waiting_on_a_ticket_still_routes_to_the_ticket_service(monkeypatch):
    github, service = _patrol(monkeypatch)
    with pytest.raises(ServiceNotFound):
        await service.can_receive_work(f"https://github.com/{REPO}/issues/42")
    assert github.requests == []


@pytest.mark.asyncio
async def test_search_failure_is_not_silently_downgraded(monkeypatch):
    github = ScriptedGitHub().on(
        "GET", "/search/issues", httpx.Response(422, json={"message": "boom"})
    )
    service = code_host(github, monkeypatch)

    with pytest.raises(ServiceError) as raised:
        await service.pull_request_candidates()

    assert str(raised.value) == t("integrations.github.response.rejected")

    with pytest.raises(ServiceError) as raised:
        await service.can_receive_work(pull_url(2))
    assert str(raised.value) == t("integrations.github.response.rejected")
