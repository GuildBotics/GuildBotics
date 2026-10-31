"""GitHub reads keep their approved destination and bounded authenticated client."""

import json
import logging
import re
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from guildbotics.entities import Person, Project, Team
from guildbotics.entities.message import Message
from guildbotics.integrations.github import api, reads
from guildbotics.integrations.github import code_hosting_service as hosting
from guildbotics.integrations.github.async_client import get_async_client
from guildbotics.integrations.github.github_utils import GitHubTokenAuth
from guildbotics.integrations.github.ticket_service import GitHubTicketService
from guildbotics.integrations.readiness import fit_log_tails
from guildbotics.runtime.port import (
    ServiceError,
    ServiceNotFound,
    ServiceUnsupported,
)
from guildbotics.runtime.resources import (
    MAX_LOG_TAIL_BYTES,
    MAX_PAGE_BYTES,
    ReadPage,
    WorkTarget,
)
from guildbotics.utils.i18n_tool import get_language, set_language, t

REPO = "GuildBotics/GuildBotics"
BASE = "https://github.example.test/api/v3/"
WEB = "https://github.example.test"
PATH = f"/api/v3/repos/{REPO}/dependabot/alerts"
PULL = f"{WEB}/{REPO}/pull/7"
ISSUE = f"{WEB}/{REPO}/issues/7"
#: Issue #7 and pull request #7, as GitHub answers each at its own address.
NAMED_ITEMS = {
    f"/api/v3/repos/{REPO}/issues/7": {"number": 7, "state": "open"},
    f"/api/v3/repos/{REPO}/pulls/7": {"number": 7, "state": "open"},
}
_RESPONSE = "integrations.github.response"


def _refused(key: str, permission: str, status: int = 0) -> str:
    """What a read refused with ``key`` says, naming the permission it needs."""
    message = t(f"{_RESPONSE}.{key}", status=status)
    if key in {"forbidden", "not_found"}:
        message += " " + t(f"{_RESPONSE}.permission", permission=permission)
    return message


@pytest.fixture(params=["en", "ja"])
def read_language(request):
    previous = get_language()
    set_language(request.param)
    try:
        yield
    finally:
        set_language(previous)


@pytest_asyncio.fixture
async def reader(monkeypatch):
    """The member's code host and tickets on a GitHub Enterprise Server,
    answered by ``state["respond"]``; the items the shared routes read first
    are answered from ``state["items"]`` and not kept in ``requests``."""
    requests = []
    state = {"respond": lambda _: httpx.Response(200, json=[]), "items": {}}

    def respond(request):
        if request.url.path in state["items"]:
            return httpx.Response(200, json=state["items"][request.url.path])
        requests.append(request)
        return state["respond"](request)

    async def create(_person, base_url, owner):
        client = get_async_client(base_url, GitHubTokenAuth("secret-value"), owner)
        client._transport = httpx.MockTransport(respond)
        client._mounts = {}
        return client

    monkeypatch.setattr(
        "guildbotics.integrations.github.api.create_github_client", create
    )
    person = Person(
        person_id="aiko",
        name="Aiko",
        account_info={"github_username": "aiko", "github_account_type": "machine_user"},
    )
    team = Team(
        project=Project(
            name="demo",
            services={
                "code_hosting_service": {"name": "github", "api_base_url": BASE},
                "ticket_service": {
                    "name": "github",
                    "api_base_url": BASE,
                    "project_id": "1",
                    "url": f"{WEB}/orgs/GuildBotics/projects/1",
                },
            },
        ),
        members=[person],
    )
    code = hosting.GitHubCodeHostingService(person, team)
    tickets = GitHubTicketService(logging.getLogger(), person, team)
    yield SimpleNamespace(code=code, tickets=tickets, state=state, requests=requests)
    await code.aclose()
    await tickets.aclose()


def readiness_payloads():
    return {
        "pulls/7": {
            "number": 7,
            "title": "PR",
            "state": "open",
            "html_url": PULL,
            "head": {"sha": "head", "ref": "topic", "repo": {"full_name": REPO}},
            "base": {"ref": "main"},
        },
        "branches/main": {"commit": {"sha": "base"}},
        "compare/base...head": {"behind_by": 0},
        "commits/head/check-runs": {
            "check_runs": [
                {"name": "test", "status": "completed", "conclusion": "failure"}
            ]
        },
        "commits/head/status": {"statuses": []},
        "actions/runs": {"workflow_runs": [{"id": 1, "conclusion": "failure"}]},
        "actions/runs/1/jobs": {
            "jobs": [{"id": 1, "name": "test", "conclusion": "failure"}]
        },
        "actions/runs/1/artifacts": {"artifacts": []},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("large_path", [*readiness_payloads(), "actions/jobs/1/logs"])
async def test_readiness_uses_host_transport_for_all_internal_reads(reader, large_path):
    payloads = readiness_payloads()

    def respond(request):
        path = request.url.path.split(f"/repos/{REPO}/")[1]
        if path.endswith("/logs"):
            return httpx.Response(
                200,
                content=b"x" * (MAX_PAGE_BYTES + 1)
                if path == large_path
                else b"failure",
            )
        payload = dict(payloads[path])
        if path == large_path:
            payload["unused_large_field"] = "x" * (MAX_PAGE_BYTES + 1)
        return httpx.Response(200, json=payload)

    reader.state["respond"] = respond
    page = await reader.code.read(
        "pull_request_readiness", PULL, parameters={"failed_logs": True}
    )
    expected = await reader.code.readiness(PULL, failed_logs=True)
    assert page.items == [expected.model_dump()]
    assert (
        page.target
        == expected.target
        == WorkTarget(kind="pull_request", url=PULL, title="PR", label="#7")
    )
    assert page.items[0]["readiness"] == "blocked"
    # A job is named by its run, the run's attempt, and its own id.
    assert [log["job_id"] for log in page.items[0]["failed_logs"]] == ["1/1/1"]
    assert len(json.dumps(page.model_dump()).encode()) <= MAX_PAGE_BYTES
    assert all("max_response_bytes" not in r.extensions for r in reader.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,headers", [("forbidden", {}), ("rate_limit", {"x-ratelimit-remaining": "0"})]
)
async def test_readiness_actions_errors_keep_structured_diagnostics(
    reader, key, headers
):
    payloads = readiness_payloads()

    def respond(request):
        path = request.url.path.split(f"/repos/{REPO}/")[1]
        return (
            httpx.Response(403, text="secret-value", headers=headers)
            if path == "commits/head/check-runs"
            else httpx.Response(200, json=payloads[path])
        )

    reader.state["respond"] = respond
    with pytest.raises(ServiceError) as error:
        await reader.code.read("pull_request_readiness", PULL)
    assert str(error.value) == _refused(
        key, "Pull requests / Contents / Checks / Commit statuses / Actions"
    )
    assert "secret-value" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["pull_request_threads", "review_thread_comments"])
@pytest.mark.parametrize(
    "kind,key,error_type",
    [
        ("FORBIDDEN", "forbidden", ServiceError),
        ("INSUFFICIENT_SCOPES", "forbidden", ServiceError),
        ("NOT_FOUND", "not_found", ServiceNotFound),
        ("RATE_LIMITED", "rate_limit", ServiceError),
        ("UNKNOWN", "response", ServiceError),
    ],
)
async def test_graphql_errors_are_safe_resource_diagnostics(
    reader, resource, kind, key, error_type, read_language
):
    reader.state["respond"] = lambda _: httpx.Response(
        200,
        json={
            "data": {"partial": "ignored"},
            "errors": [{"type": kind, "message": "secret-value"}],
        },
    )
    with pytest.raises(error_type) as error:
        await reader.code.read(
            resource,
            PULL,
            parameters={"node": "thread"}
            if resource == "review_thread_comments"
            else {},
        )
    assert str(error.value) == t(f"{_RESPONSE}.{key}")
    assert "secret-value" not in str(error.value)


@pytest.mark.parametrize("text", ["\x1b[31m\x00\n", "日本語🙂"])
@pytest.mark.parametrize("state", ["open", "closed"])
def test_log_budget_counts_escaped_tails_and_repeated_metadata(text, state):
    target = WorkTarget(kind="pull_request", url=PULL, title="PR", label="#7")
    result = {
        "target": target.model_dump(),
        "readiness": "blocked" if state == "open" else "not_applicable",
        "checks": [{"name": "test"}],
        "failed_logs": [
            {
                "log": text * 10000,
                "log_bytes": len((text * 10000).encode()),
                "tail_limit_bytes": MAX_LOG_TAIL_BYTES,
                "truncated": False,
                "artifact_names": ["artifact" * 1000],
                "name": "job" + str(i),
            }
            for i in range(12)
        ],
    }
    fit_log_tails(result)
    assert (
        len(json.dumps(ReadPage(items=[result], target=target).model_dump()).encode())
        <= MAX_PAGE_BYTES
    )
    assert len(result["failed_logs"]) == 12
    assert all(
        item["truncated"] and item["log"] and (text * 10000).endswith(item["log"])
        for item in result["failed_logs"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch,additions,deletions,complete",
    [
        ("@@ -1 +1 @@\n-old\n+new", 1, 1, True),
        ("@@ -1 +1 @@\n-old\n+new", 2, 1, False),
        (None, 0, 0, False),
    ],
)
async def test_missing_or_truncated_patch_is_not_complete(
    reader, patch, additions, deletions, complete
):
    item = {"filename": "file", "additions": additions, "deletions": deletions}
    if patch is not None:
        item["patch"] = patch
    reader.state["respond"] = lambda _: httpx.Response(200, json=[item])
    result = await reader.code.read("pull_request_files", PULL)
    assert result.items[0]["patch_complete"] is complete
    assert result.items[0]["patch_available"] is (patch is not None)


_COMMENT = {
    "id": 1,
    "body": "comment",
    "user": {"login": "aiko"},
    "created_at": "2026-01-01T00:00:00Z",
    "html_url": f"{PULL}#issuecomment-1",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service,resource,subject,suffix,item,read",
    [
        (
            "code",
            "pull_request_comments",
            PULL,
            "issues/7/comments",
            _COMMENT,
            {
                "id": "issuecomment-1",
                "body": "comment",
                "author": "aiko",
                "author_type": Message.ASSISTANT,
                "created_at": "2026-01-01T00:00:00Z",
                "url": f"{PULL}#issuecomment-1",
            },
        ),
        (
            "tickets",
            "ticket_comments",
            ISSUE,
            "issues/7/comments",
            {**_COMMENT, "user": {"login": "someone"}},
            {
                "id": "issuecomment-1",
                "body": "comment",
                "author": "someone",
                "author_type": Message.USER,
                "created_at": "2026-01-01T00:00:00Z",
                "url": f"{PULL}#issuecomment-1",
            },
        ),
        (
            "code",
            "pull_request_reviews",
            PULL,
            "pulls/7/reviews",
            {
                "id": 1,
                "body": "review",
                "state": "CHANGES_REQUESTED",
                "commit_id": "abc",
                "user": {"login": "someone"},
            },
            {
                "id": "pullrequestreview-1",
                "body": "review",
                "author": "someone",
                "author_type": Message.USER,
                "verdict": "request_changes",
                "commit": "abc",
                "submitted_at": None,
                "url": None,
            },
        ),
        (
            "code",
            "pull_request_files",
            PULL,
            "pulls/7/files",
            {"filename": "a.py", "patch": "@@ -1 +1 @@\n-old\n+new"},
            None,
        ),
        (
            "code",
            "ticket_pull_requests",
            ISSUE,
            "issues/7/timeline",
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {"merged_at": "2026-01-02T00:00:00Z"},
                        "html_url": f"{WEB}/fork/repo/pull/9",
                        "title": "Fix",
                        "state": "closed",
                    }
                },
            },
            {
                "url": f"{WEB}/fork/repo/pull/9",
                "label": "fork/repo#9",
                "title": "Fix",
                "state": "closed",
                "merged": True,
            },
        ),
    ],
)
async def test_inspection_rest_pages_rebuild_original_routes(
    reader, service, resource, subject, suffix, item, read
):
    reader.state["items"] = NAMED_ITEMS
    reader.state["respond"] = lambda _: httpx.Response(
        200,
        json=[item],
        headers={"Link": '<https://evil.test/secret?page=2&method=DELETE>; rel="next"'},
    )
    first = await getattr(reader, service).read(resource, subject)
    reader.state["respond"] = lambda _: httpx.Response(200, json=[])
    second = await getattr(reader, service).read(
        resource, subject, continuation=first.continuation
    )
    assert not second.items and not second.continuation
    assert all(
        r.method == "GET"
        and r.url.host == "github.example.test"
        and r.url.path == f"/api/v3/repos/{REPO}/{suffix}"
        for r in reader.requests
    )
    assert reader.requests[-1].url.params["page"] == "2"
    if read is not None:
        assert first.items == [read]
    if resource == "pull_request_files":
        assert first.items[0]["commentable_lines"] == [
            {"line": 1, "side": "LEFT", "left_line": 1},
            {"line": 1, "side": "RIGHT", "right_line": 1},
        ]


@pytest.mark.asyncio
async def test_an_unsubmitted_review_and_an_event_that_is_no_pull_request_are_left_out(
    reader,
):
    reader.state["items"] = NAMED_ITEMS
    reader.state["respond"] = lambda request: httpx.Response(
        200,
        json=[{"id": 2, "state": "PENDING"}, {"id": 3, "state": "DISMISSED"}]
        if request.url.path.endswith("/reviews")
        else [
            {"event": "labeled"},
            {"event": "cross-referenced", "source": {"issue": {"html_url": ISSUE}}},
            # A pull request of another GitHub.
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "html_url": "https://github.com/other/repo/pull/1",
                    }
                },
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {},
                        "html_url": f"{WEB}/{REPO.upper()}/pull/8",
                        "title": "Same repository",
                        "state": "open",
                    }
                },
            },
        ],
    )

    reviews = await reader.code.read("pull_request_reviews", PULL)
    referring = await reader.code.read("ticket_pull_requests", ISSUE)

    assert [(r["id"], r["verdict"]) for r in reviews.items] == [
        ("pullrequestreview-3", "dismissed")
    ]
    # One of the same repository is labeled as GitHub shows it there.
    assert referring.items == [
        {
            "url": f"{WEB}/{REPO.upper()}/pull/8",
            "label": "#8",
            "title": "Same repository",
            "state": "open",
            "merged": False,
        }
    ]


@pytest.mark.asyncio
async def test_the_pull_requests_of_a_ticket_elsewhere_are_unsupported(reader):
    with pytest.raises(ServiceUnsupported):
        await reader.code.read(
            "ticket_pull_requests", "https://tickets.example.com/browse/OPS-7"
        )
    # A pull request is not a ticket.
    with pytest.raises(ServiceNotFound):
        await reader.code.read("ticket_pull_requests", PULL)
    assert reader.requests == []


@pytest.mark.asyncio
async def test_every_page_of_a_shared_route_reads_its_items_kind(reader):
    """A continuation names no more than the request it continues: the page
    it asks for is of an issue only while the number is still an issue's."""
    reader.state["items"] = dict(NAMED_ITEMS)
    reader.state["respond"] = lambda _: httpx.Response(
        200,
        json=[_COMMENT],
        headers={"Link": f'<{BASE}repos/{REPO}/issues/7/comments?page=2>; rel="next"'},
    )
    first = await reader.tickets.read("ticket_comments", ISSUE)
    reader.state["items"][f"/api/v3/repos/{REPO}/issues/7"] = {
        "number": 7,
        "pull_request": {},
    }

    with pytest.raises(ServiceNotFound, match="is a pull request"):
        await reader.tickets.read(
            "ticket_comments", ISSUE, continuation=first.continuation
        )
    assert len(reader.requests) == 1


@pytest.mark.asyncio
async def test_detail_names_host_observed_target_and_fork_head(reader):
    reader.state["respond"] = lambda _: httpx.Response(
        200,
        json={
            "number": 7,
            "title": "Target",
            "body": None,
            "state": "open",
            "html_url": f"{WEB}/a/b/pull/7",
            "user": {"login": "aiko"},
            "head": {
                "sha": "head",
                "ref": "feature",
                "repo": {"full_name": "fork/repo"},
            },
            "base": {"ref": "main"},
            "assignees": [{"login": "aiko"}],
            "labels": [{"name": "bug"}],
            "merged_at": None,
            "draft": True,
            "changed_files": 3,
        },
    )
    page = await reader.code.read("pull_requests", PULL)
    target = WorkTarget(
        kind="pull_request", url=f"{WEB}/a/b/pull/7", title="Target", label="#7"
    )
    assert page.target == target
    assert page.items == [
        {
            **target.model_dump(),
            "repository": REPO,
            "body": "",
            "state": "open",
            "assignees": ["aiko"],
            "author": "aiko",
            "requested_reviewers": [],
            "labels": ["bug"],
            "author_type": Message.ASSISTANT,
            "head": "feature",
            "head_repository": "fork/repo",
            "head_sha": "head",
            "base": "main",
            "merged": False,
            "draft": True,
            "changed_files": 3,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource,subject,parameters",
    [
        ("pull_requests", PULL, {"page_size": 1}),
        ("pull_requests", PULL, {"query": "mutation {}"}),
        ("pull_request_threads", PULL, {"node": "thread"}),
        ("review_thread_comments", PULL, {}),
        ("pull_request_readiness", PULL, {"failed_logs": "true"}),
        ("pull_request_readiness", PULL, {"log_tail_bytes": MAX_LOG_TAIL_BYTES + 1}),
        # The ticket service's reads are not the code host's.
        ("tickets", ISSUE, {}),
        ("ticket_comments", ISSUE, {}),
        ("secrets", PULL, {}),
        # A pull request's resources are named by its URL on this GitHub.
        ("pull_requests", ISSUE, {}),
        ("pull_requests", REPO, {}),
        ("pull_requests", f"{PULL}?x=1", {}),
        ("pull_requests", f"{PULL}#discussion_r1", {}),
        ("pull_requests", f"https://github.com/{REPO}/pull/7", {}),
        ("pull_requests", f"{WEB}/{REPO}/pull/0", {}),
        ("pull_requests", f"{WEB}/{REPO}/pull/7/files", {}),
        ("pull_requests", f"{WEB}/a/../pull/7", {}),
    ],
)
async def test_resource_conditions_rejected_before_network(
    reader, resource, subject, parameters
):
    with pytest.raises(ServiceError):
        await reader.code.read(resource, subject, parameters=parameters)
    assert reader.requests == []


def _connection(nodes, cursor=None):
    return {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor},
    }


@pytest.mark.asyncio
async def test_fixed_graphql_pages_are_bound_to_the_same_conditions(reader):
    def threads(cursor=None):
        return lambda _: httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequest": {"reviewThreads": _connection([], cursor)}
                    }
                }
            },
        )

    reader.state["respond"] = threads("next")
    page = await reader.code.read(
        "pull_request_threads", PULL, parameters={"page_size": 2}
    )
    reader.state["respond"] = threads()
    await reader.code.read(
        "pull_request_threads",
        PULL,
        parameters={"page_size": 2},
        continuation=page.continuation,
    )
    sent = [json.loads(r.content) for r in reader.requests]
    assert sent[0]["query"] == sent[1]["query"] and "mutation" not in sent[0]["query"]
    assert sent[1]["variables"] == {
        "owner": "GuildBotics",
        "repo": "GuildBotics",
        "number": 7,
        "after": "next",
        "size": 2,
    }
    for changed in (
        {"subject": f"{WEB}/{REPO}/pull/8"},
        {"subject": f"{WEB}/other/repo/pull/7"},
        {"parameters": {"page_size": 3}},
    ):
        with pytest.raises(ServiceError):
            await reader.code.read(
                **{
                    "resource": "pull_request_threads",
                    "subject": PULL,
                    "parameters": {"page_size": 2},
                    "continuation": page.continuation,
                    **changed,
                }
            )
    assert len(reader.requests) == 2


@pytest.mark.asyncio
async def test_nested_thread_comments_verify_parent_and_paginate(reader):
    def payload(parent="pr7", cursor=None):
        return {
            "data": {
                "repository": {"pullRequest": {"id": "pr7"}},
                "node": {
                    "pullRequest": {"id": parent},
                    "comments": _connection(
                        [
                            {"databaseId": 10, "body": "root", "replyTo": None},
                            {
                                "databaseId": 11,
                                "body": "reply",
                                "replyTo": {"databaseId": 10},
                            },
                        ],
                        cursor,
                    ),
                },
            }
        }

    reader.state["respond"] = lambda _: httpx.Response(200, json=payload(cursor="more"))
    first = await reader.code.read(
        "review_thread_comments", PULL, parameters={"node": "thread1"}
    )
    assert [(c["id"], c["reply_to_id"]) for c in first.items] == [
        ("discussion_r10", None),
        ("discussion_r11", "discussion_r10"),
    ]
    reader.state["respond"] = lambda _: httpx.Response(200, json=payload())
    last = await reader.code.read(
        "review_thread_comments",
        PULL,
        parameters={"node": "thread1"},
        continuation=first.continuation,
    )
    assert not last.continuation
    reader.state["respond"] = lambda _: httpx.Response(
        200, json=payload(parent="other-pr")
    )
    with pytest.raises(ServiceNotFound):
        await reader.code.read(
            "review_thread_comments", PULL, parameters={"node": "other-thread"}
        )


@pytest.mark.asyncio
async def test_large_thread_returns_metadata_before_comment_pagination(reader):
    def respond(request):
        query = json.loads(request.content)["query"]
        size = int(re.search(r"comments\(first:(\d+)\)", query)[1])
        comments = [
            {"databaseId": i + 1, "body": "x" * 60000, "replyTo": None}
            for i in range(size)
        ]
        thread = {
            "id": "thread",
            "isResolved": False,
            "isOutdated": False,
            "comments": _connection(comments, "next"),
        }
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequest": {"reviewThreads": _connection([thread])}
                    }
                }
            },
        )

    reader.state["respond"] = respond
    page = await reader.code.read(
        "pull_request_threads", PULL, parameters={"page_size": 1}
    )
    assert page.items[0]["comments_complete"] is False
    assert len(page.items[0]["comments"]) == 1


@pytest.mark.parametrize(
    "code_url,expected",
    [
        ("https://enterprise.test/api/v3/", "https://enterprise.test/api/v3"),
        ("", "https://api.github.com"),
    ],
)
def test_reads_and_writes_use_the_configured_code_host_endpoint(code_url, expected):
    person = Person(person_id="aiko", name="Aiko")
    team = Team(
        members=[],
        project=Project(
            name="demo",
            services={
                "code_hosting_service": {"name": "github", "api_base_url": code_url},
                "ticket_service": {
                    "name": "github",
                    "api_base_url": "https://old.test/api/v3",
                },
            },
        ),
    )
    assert hosting.GitHubCodeHostingService(person, team).base_url == expected


@pytest.mark.asyncio
async def test_graphql_partial_data_fails_explicitly(reader):
    reader.state["respond"] = lambda _: httpx.Response(
        200, json={"errors": [{"message": "private upstream detail"}], "data": {}}
    )
    with pytest.raises(ServiceError) as error:
        await reader.code.read("pull_request_threads", PULL)
    assert "private upstream detail" not in str(error.value)


async def read(service, **overrides):
    return await service.read(
        **{"resource": "dependency_alerts", "subject": REPO, **overrides}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"resource": "secrets"},
        {"resource": "https://evil.test"},
        {"subject": "https://github.com/a/b"},
        {"subject": "a/.."},
        {"subject": "a/b/../../secrets"},
        {"subject": "a/b/c"},
        {"subject": "a/b?path=x"},
        {"subject": "a/b%2fsecret"},
        {"subject": "a/b#fragment"},
        {"parameters": {"id": "../secrets"}},
        {"parameters": {"id": "0"}},
        {"parameters": {"method": "PATCH"}},
        {"parameters": {"headers": {"Authorization": "x"}}},
        {"parameters": {"query": "mutation {}"}},
        {"parameters": {"after": "cursor"}},
        {"parameters": {"page_size": 0}},
        {"parameters": {"page_size": "30"}},
        {"parameters": {"id": "1", "state": "open"}},
        {"parameters": ["id"]},
        {"parameters": {"id": "1"}, "continuation": "cursor"},
    ],
)
async def test_unapproved_requests_never_reach_network(reader, overrides):
    with pytest.raises(ServiceError):
        await read(reader.code, **overrides)
    assert not reader.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link",
    [
        "https://github.example.test/api/v3/repositories/123/dependabot/alerts?per_page=30&after=cursor%2B1",
        "https://api.github.com/repositories/123/dependabot/alerts?after=cursor%2B1",
        "https://evil.test/other?state=open&per_page=99&after=cursor%2B1&method=PATCH",
        "https://user:password@api.github.com/other?after=cursor%2B1#fragment",
    ],
)
async def test_link_contributes_only_cursor_to_original_route_and_conditions(
    reader, link
):
    reader.state["respond"] = lambda _: httpx.Response(
        200, json=[], headers={"Link": f'<{link}>; rel="next"'}
    )
    first = await read(reader.code, parameters={"state": "resolved", "page_size": 1})
    assert len(reader.requests) == 1
    assert first.continuation
    reader.state["respond"] = lambda _: httpx.Response(200, json=[])
    second = await read(
        reader.code,
        parameters={"state": "resolved", "page_size": 1},
        continuation=first.continuation,
    )
    assert second.items == [] and second.continuation is None
    assert len(reader.requests) == 2
    assert all(
        request.method == "GET"
        and request.url.host == "github.example.test"
        and request.url.path == PATH
        for request in reader.requests
    )
    assert dict(reader.requests[1].url.params) == {
        "state": "fixed",
        "per_page": "1",
        "after": "cursor+1",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["page=2", "after=", "after=a&after=b", "after=" + "x" * 2049]
)
async def test_missing_ambiguous_or_oversized_link_cursor_is_rejected(reader, query):
    reader.state["respond"] = lambda _: httpx.Response(
        200,
        json=[],
        headers={
            "Link": f'<https://api.github.com/repositories/123/dependabot/alerts?{query}>; rel="next"'
        },
    )
    with pytest.raises(
        ServiceError, match=re.escape(t("integrations.resources.continuation"))
    ):
        await read(reader.code)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "continuation",
    ["https://evil.test", "garbage", "e30=", "W10=", "bnVsbA==", "x" * 8193],
)
async def test_malformed_continuation_never_reaches_network(reader, continuation):
    with pytest.raises(ServiceError):
        await read(reader.code, continuation=continuation)
    assert not reader.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"subject": "other/repo"},
        {"parameters": {"state": "resolved"}},
        {"parameters": {"page_size": 1}},
        {"resource": "secrets"},
        {"api_base_url": "https://other.test/api/v3/"},
    ],
)
async def test_real_continuation_is_bound_to_original_request_and_api(
    reader, overrides
):
    reader.state["respond"] = lambda _: httpx.Response(
        200,
        json=[],
        headers={
            "Link": '<https://api.github.com/repositories/123/dependabot/alerts?after=cursor>; rel="next"'
        },
    )
    first = await read(reader.code)
    overrides = dict(overrides)
    if "api_base_url" in overrides:
        await reader.code.aclose()
        reader.code.base_url = overrides.pop("api_base_url")
    with pytest.raises(ServiceError):
        await read(reader.code, continuation=first.continuation, **overrides)
    assert len(reader.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "key", "error_type"),
    [
        (401, {}, "authentication", ServiceError),
        (403, {}, "forbidden", ServiceError),
        (404, {}, "not_found", ServiceNotFound),
        (422, {}, "rejected", ServiceError),
        (429, {}, "rate_limit", ServiceError),
        (403, {"x-ratelimit-remaining": "0"}, "rate_limit", ServiceError),
        (403, {"retry-after": "60"}, "rate_limit", ServiceError),
        (500, {}, "http", ServiceError),
        # A redirect is not followed: its body is no page of alerts.
        (302, {"location": "https://evil.test"}, "response", ServiceError),
    ],
)
async def test_production_hook_errors_are_sanitized_and_redirects_not_followed(
    reader, status, headers, key, error_type, read_language
):
    reader.state["respond"] = lambda _: httpx.Response(
        status, text="secret-value", headers=headers
    )
    with pytest.raises(error_type) as error:
        await read(reader.code)
    expected = _refused(key, "Dependabot alerts", status)
    if status == 404:
        expected += " " + t(f"{_RESPONSE}.alerts_disabled")
    assert str(error.value) == expected
    assert "secret-value" not in str(error.value)
    assert len(reader.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"bad json", b"null", b"{}", b"[null]"])
async def test_bad_collection_shapes_fail(reader, body):
    reader.state["respond"] = lambda _: httpx.Response(200, content=body)
    with pytest.raises(ServiceError, match=re.escape(t(f"{_RESPONSE}.response"))):
        await read(reader.code)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service,resource,subject,parameters",
    [
        ("code", "dependency_alerts", REPO, {}),
        ("code", "dependency_alerts", REPO, {"id": "7"}),
        ("code", "pull_request_comments", PULL, {}),
        ("code", "pull_request_reviews", PULL, {}),
        ("code", "pull_request_files", PULL, {}),
        ("code", "ticket_pull_requests", ISSUE, {}),
        ("code", "pull_request_threads", PULL, {}),
        ("code", "review_thread_comments", PULL, {"node": "thread"}),
        ("tickets", "ticket_comments", ISSUE, {}),
    ],
)
async def test_raw_limit_is_enforced_by_authenticated_client(
    reader, monkeypatch, service, resource, subject, parameters
):
    reader.state["items"] = NAMED_ITEMS
    monkeypatch.setattr(reads, "MAX_PAGE_BYTES", 250)
    reader.state["respond"] = lambda _: httpx.Response(200, content=b"x" * 251)
    with pytest.raises(ServiceError, match=re.escape(t(f"{_RESPONSE}.too_large"))):
        await getattr(reader, service).read(resource, subject, parameters=parameters)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service,resource,subject",
    [("code", "pull_requests", PULL), ("tickets", "tickets", ISSUE)],
)
async def test_detail_read_limit_is_enforced_by_authenticated_client(
    reader, monkeypatch, service, resource, subject
):
    monkeypatch.setattr(api, "MAX_PAGE_BYTES", 250)
    reader.state["respond"] = lambda _: httpx.Response(200, json={"body": "x" * 251})
    with pytest.raises(ServiceError, match=re.escape(t(f"{_RESPONSE}.too_large"))):
        await getattr(reader, service).read(resource, subject)


@pytest.mark.asyncio
async def test_detail_and_transport_failure(reader):
    reader.state["respond"] = lambda _: httpx.Response(
        200, json={"number": 42, "state": "open"}
    )
    result = await read(reader.code, parameters={"id": "42"})
    assert result.items[0]["id"] == "42" and result.continuation is None
    assert reader.requests[0].url.path == PATH + "/42"
    assert not reader.requests[0].url.query

    def fail(request):
        raise httpx.ConnectError("secret-value", request=request)

    reader.state["respond"] = fail
    with pytest.raises(ServiceError, match=re.escape(t(f"{_RESPONSE}.transport"))):
        await read(reader.code)
