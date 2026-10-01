"""GitHub reads keep their approved destination and bounded authenticated client."""

import httpx
import json
import pytest
import pytest_asyncio

from guildbotics.entities import Person, Project, Team
from guildbotics.integrations.code_hosting_service import RepositoryReadError
from guildbotics.integrations.github import code_hosting_service as hosting
from guildbotics.integrations.github.async_client import get_async_client
from guildbotics.integrations.github.github_utils import GitHubTokenAuth
from guildbotics.utils.i18n_tool import get_language, set_language, t

REPO = "GuildBotics/GuildBotics"
BASE = "https://github.example.test/api/v3/"
PATH = f"/api/v3/repos/{REPO}/dependabot/alerts"


@pytest.fixture(params=["en", "ja"])
def read_language(request):
    previous = get_language()
    set_language(request.param)
    try:
        yield
    finally:
        set_language(previous)


def readiness_payloads():
    return {
        "pulls/7": {
            "number": 7,
            "title": "PR",
            "state": "open",
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
    from guildbotics.capabilities.member_github import MemberGitHubCapabilityService
    from guildbotics.integrations.code_hosting_service import MAX_PAGE_BYTES

    service, state, requests = reader
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

    state["respond"] = respond
    page = await service.read(
        "pull_request_readiness", REPO, identifier="7", parameters={"failed_logs": True}
    )
    host = MemberGitHubCapabilityService(service.person, service.team)
    try:
        expected = await host.pr_checks(
            f"https://github.example.test/{REPO}/pull/7", failed_logs=True
        )
    finally:
        await host.aclose()
    assert page.items == [expected]
    assert page.items[0]["readiness"] == "blocked"
    assert len(json.dumps(page.model_dump()).encode()) <= MAX_PAGE_BYTES
    assert all("max_response_bytes" not in r.extensions for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,headers", [("forbidden", {}), ("rate_limit", {"x-ratelimit-remaining": "0"})]
)
async def test_readiness_actions_errors_keep_structured_diagnostics(
    reader, key, headers
):
    service, state, _ = reader
    payloads = readiness_payloads()

    def respond(request):
        path = request.url.path.split(f"/repos/{REPO}/")[1]
        return (
            httpx.Response(403, text="secret-value", headers=headers)
            if path == "commits/head/check-runs"
            else httpx.Response(200, json=payloads[path])
        )

    state["respond"] = respond
    with pytest.raises(RepositoryReadError) as error:
        await service.read("pull_request_readiness", REPO, identifier="7")
    assert str(error.value) == t(
        f"integrations.github.read.{key}",
        permission="Pull requests / Contents / Checks / Commit statuses / Actions",
    )
    assert "secret-value" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource,permission",
    [
        ("issue_projects", "Projects"),
        ("pull_request_threads", "Pull requests"),
        ("review_thread_comments", "Pull requests"),
    ],
)
@pytest.mark.parametrize(
    "kind,key",
    [
        ("FORBIDDEN", "forbidden"),
        ("INSUFFICIENT_SCOPES", "forbidden"),
        ("NOT_FOUND", "not_found"),
        ("RATE_LIMITED", "rate_limit"),
        ("UNKNOWN", "response"),
    ],
)
async def test_graphql_errors_are_safe_resource_diagnostics(
    reader, resource, permission, kind, key, read_language
):
    service, state, _ = reader
    state["respond"] = lambda _: httpx.Response(
        200,
        json={
            "data": {"partial": "ignored"},
            "errors": [{"type": kind, "message": "secret-value"}],
        },
    )
    with pytest.raises(RepositoryReadError) as error:
        await service.read(
            resource,
            REPO,
            identifier="7",
            parameters={"node": "thread"}
            if resource == "review_thread_comments"
            else {},
        )
    assert str(error.value) == t(
        f"integrations.github.read.{key}", permission=permission
    )
    assert "secret-value" not in str(error.value)


@pytest.mark.parametrize("text", ["\x1b[31m\x00\n", "日本語🙂"])
@pytest.mark.parametrize("state", ["open", "closed"])
def test_log_budget_counts_escaped_tails_and_repeated_metadata(text, state):
    from guildbotics.integrations.github.pull_requests import _fit_log_tails
    from guildbotics.integrations.code_hosting_service import (
        MAX_PAGE_BYTES,
        RepositoryReadPage,
    )

    result = {
        "target": {"title": "PR"},
        "readiness": "blocked" if state == "open" else "not_applicable",
        "checks": [{"name": "test"}],
        "failed_logs": [
            {
                "log": text * 10000,
                "log_bytes": len((text * 10000).encode()),
                "tail_limit_bytes": 65536,
                "truncated": False,
                "artifact_names": ["artifact" * 1000],
                "name": "job" + str(i),
            }
            for i in range(12)
        ],
    }
    _fit_log_tails(result)
    assert (
        len(
            json.dumps(
                RepositoryReadPage(items=[result], target=result["target"]).model_dump()
            ).encode()
        )
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
    service, state, _ = reader
    item = {"filename": "file", "additions": additions, "deletions": deletions}
    if patch is not None:
        item["patch"] = patch
    state["respond"] = lambda _: httpx.Response(200, json=[item])
    result = await service.read("pull_request_files", REPO, identifier="7")
    assert result.items[0]["patch_complete"] is complete
    assert result.items[0]["patch_available"] is (patch is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource,suffix,item",
    [
        (
            "issue_comments",
            "issues/7/comments",
            {"id": 1, "body": "comment", "user": {"login": "aiko"}},
        ),
        (
            "pull_request_reviews",
            "pulls/7/reviews",
            {"id": 1, "body": "review", "state": "APPROVED"},
        ),
        (
            "pull_request_files",
            "pulls/7/files",
            {"filename": "a.py", "patch": "@@ -1 +1 @@\n-old\n+new"},
        ),
        (
            "issue_timeline",
            "issues/7/timeline",
            {
                "source": {
                    "issue": {
                        "pull_request": {},
                        "html_url": "https://github.com/fork/repo/pull/9",
                    }
                }
            },
        ),
    ],
)
async def test_inspection_rest_pages_rebuild_original_routes(
    reader, resource, suffix, item
):
    service, state, requests = reader
    state["respond"] = lambda _: httpx.Response(
        200,
        json=[item],
        headers={"Link": '<https://evil.test/secret?page=2&method=DELETE>; rel="next"'},
    )
    first = await service.read(resource, REPO, identifier="7")
    state["respond"] = lambda _: httpx.Response(200, json=[])
    second = await service.read(
        resource, REPO, identifier="7", continuation=first.continuation
    )
    assert not second.items and not second.continuation
    assert all(
        r.method == "GET"
        and r.url.host == "github.example.test"
        and r.url.path == f"/api/v3/repos/{REPO}/{suffix}"
        for r in requests
    )
    assert requests[-1].url.params["page"] == "2"
    if resource == "pull_request_files":
        assert first.items[0]["commentable_lines"] == [
            {
                "line": 1,
                "side": "LEFT",
                "left_line": 1,
            },
            {
                "line": 1,
                "side": "RIGHT",
                "right_line": 1,
            },
        ]
    if resource == "issue_timeline":
        assert first.items[0]["pull_request"] == {"repo": "fork/repo", "number": 9}


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["issues", "pull_requests"])
async def test_detail_names_host_observed_target_and_fork_head(reader, resource):
    service, state, _ = reader
    state["respond"] = lambda _: httpx.Response(
        200,
        json={
            "number": 7,
            "title": "Target",
            "body": None,
            "state": "open",
            "html_url": "https://github.example.test/a/b/pull/7",
            "head": {
                "sha": "head",
                "ref": "feature",
                "repo": {"full_name": "fork/repo"},
            },
            "base": {"ref": "main"},
            "assignees": [{"login": "aiko"}],
            "labels": [{"name": "bug"}],
        },
    )
    page = await service.read(resource, REPO, identifier="7")
    assert page.target["title"] == "Target" and page.target["number"] == 7
    assert page.items[0]["assignees"] == ["aiko"] and page.items[0]["labels"] == ["bug"]
    if resource == "pull_requests":
        assert page.items[0]["head_repo"] == "fork/repo"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource,parameters",
    [
        ("issues", {"page_size": 1}),
        ("pull_requests", {"query": "mutation {}"}),
        ("pull_request_threads", {"node": "thread"}),
        ("review_thread_comments", {}),
        ("pull_request_readiness", {"failed_logs": "true"}),
        ("pull_request_readiness", {"log_tail_bytes": 65537}),
    ],
)
async def test_resource_conditions_rejected_before_network(
    reader, resource, parameters
):
    service, _, requests = reader
    with pytest.raises(RepositoryReadError):
        await service.read(resource, REPO, identifier="7", parameters=parameters)
    assert requests == []


def _connection(nodes, cursor=None):
    return {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource,kind,connection",
    [
        ("issue_projects", "issue", "projectItems"),
        ("pull_request_threads", "pullRequest", "reviewThreads"),
    ],
)
async def test_fixed_graphql_pages_are_bound_to_the_same_conditions(
    reader, resource, kind, connection
):
    service, state, requests = reader
    state["respond"] = lambda _: httpx.Response(
        200,
        json={"data": {"repository": {kind: {connection: _connection([], "next")}}}},
    )
    page = await service.read(
        resource, REPO, identifier="7", parameters={"page_size": 2}
    )
    import json

    state["respond"] = lambda _: httpx.Response(
        200, json={"data": {"repository": {kind: {connection: _connection([])}}}}
    )
    await service.read(
        resource,
        REPO,
        identifier="7",
        parameters={"page_size": 2},
        continuation=page.continuation,
    )
    sent = [json.loads(r.content) for r in requests]
    assert sent[0]["query"] == sent[1]["query"] and "mutation" not in sent[0]["query"]
    assert sent[1]["variables"] == {
        "owner": "GuildBotics",
        "repo": "GuildBotics",
        "number": 7,
        "after": "next",
        "size": 2,
    }
    for changed in (
        {"identifier": "8"},
        {"repo": "other/repo"},
        {"parameters": {"page_size": 3}},
    ):
        with pytest.raises(RepositoryReadError):
            await service.read(
                **{
                    "resource": resource,
                    "repo": REPO,
                    "identifier": "7",
                    "parameters": {"page_size": 2},
                    "continuation": page.continuation,
                    **changed,
                }
            )
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_nested_thread_comments_verify_parent_and_paginate(reader):
    service, state, _ = reader

    def payload(parent="pr7", cursor=None):
        return {
            "data": {
                "repository": {"pullRequest": {"id": "pr7"}},
                "node": {
                    "pullRequest": {"id": parent},
                    "comments": _connection(
                        [{"databaseId": 10, "body": "root", "replyTo": None}], cursor
                    ),
                },
            }
        }

    state["respond"] = lambda _: httpx.Response(200, json=payload(cursor="more"))
    first = await service.read(
        "review_thread_comments", REPO, identifier="7", parameters={"node": "thread1"}
    )
    assert first.items[0]["reply_to_id"] is None and first.items[0]["id"] == 10
    state["respond"] = lambda _: httpx.Response(200, json=payload())
    last = await service.read(
        "review_thread_comments",
        REPO,
        identifier="7",
        parameters={"node": "thread1"},
        continuation=first.continuation,
    )
    assert not last.continuation
    state["respond"] = lambda _: httpx.Response(200, json=payload(parent="other-pr"))
    with pytest.raises(RepositoryReadError):
        await service.read(
            "review_thread_comments",
            REPO,
            identifier="7",
            parameters={"node": "other-thread"},
        )


@pytest.mark.asyncio
async def test_large_thread_returns_metadata_before_comment_pagination(reader):
    import re

    service, state, _ = reader

    def respond(request):
        query = json.loads(request.content)["query"]
        size = int(re.search(r"comments\(first:(\d+)\)", query)[1])
        comments = [
            {"databaseId": i, "body": "x" * 60000, "replyTo": None} for i in range(size)
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

    state["respond"] = respond
    page = await service.read(
        "pull_request_threads", REPO, identifier="7", parameters={"page_size": 1}
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
def test_common_reads_and_member_actions_share_the_configured_endpoint(
    code_url, expected
):
    from guildbotics.capabilities.member_github import MemberGitHubCapabilityService

    person = Person(person_id="aiko", name="Aiko")
    team = Team(
        members=[],
        project=Project(
            name="demo",
            services={
                "code_hosting_service": {"name": "github", "api_base_url": code_url},
                "ticket_manager": {
                    "name": "github",
                    "base_url": "https://old.test/api/v3",
                },
            },
        ),
    )
    assert hosting.GitHubCodeHostingService(person, team).base_url == expected
    assert MemberGitHubCapabilityService(person, team).base_url == expected


@pytest.mark.asyncio
async def test_graphql_partial_data_and_project_field_overflow_fail_explicitly(reader):
    service, state, _ = reader
    project = {
        "id": "item",
        "project": {"title": "Board"},
        "fieldValues": _connection([], "more"),
    }
    for payload in [
        {"errors": [{"message": "private upstream detail"}], "data": {}},
        {"data": {"repository": {"issue": {"projectItems": _connection([project])}}}},
    ]:
        state["respond"] = lambda _, p=payload: httpx.Response(200, json=p)
        with pytest.raises(RepositoryReadError) as error:
            await service.read("issue_projects", REPO, identifier="7")
        assert "private upstream detail" not in str(error.value)


@pytest.mark.asyncio
async def test_common_readiness_is_the_host_service_used_by_push_and_completion(
    reader, monkeypatch
):
    from guildbotics.integrations.github.pull_requests import GitHubPullRequests
    from guildbotics.capabilities.member_github import MemberGitHubCapabilityService

    service, _, _ = reader
    assert MemberGitHubCapabilityService.pr_checks is GitHubPullRequests.pr_checks
    observed = []

    async def checks(self, url, **kwargs):
        observed.append((url, kwargs))
        return {
            "target": {"title": "host target"},
            "readiness": "blocked",
            "completion_blockers": [{"code": "head_changed"}],
        }

    monkeypatch.setattr(GitHubPullRequests, "pr_checks", checks)
    page = await service.read(
        "pull_request_readiness", REPO, identifier="7", parameters={"failed_logs": True}
    )
    assert page.items[0]["readiness"] == "blocked" and page.target == {
        "title": "host target"
    }
    assert observed == [
        (
            f"https://github.example.test/{REPO}/pull/7",
            {"failed_logs": True, "log_tail_bytes": 65536},
        )
    ]


@pytest_asyncio.fixture
async def reader(monkeypatch):
    requests = []
    state = {"respond": lambda _: httpx.Response(200, json=[])}

    def respond(request):
        requests.append(request)
        return state["respond"](request)

    async def create(_person, base_url, owner):
        client = get_async_client(
            base_url,
            GitHubTokenAuth("secret-value"),
            owner,
        )
        client._transport = httpx.MockTransport(respond)
        client._mounts = {}
        return client

    monkeypatch.setattr(
        "guildbotics.integrations.github.pull_requests.create_github_client", create
    )
    service = hosting.GitHubCodeHostingService(
        Person(
            person_id="aiko",
            name="Aiko",
            account_info={
                "github_username": "aiko",
                "github_account_type": "machine_user",
            },
        ),
        Team(
            project=Project(
                name="demo",
                services={
                    "code_hosting_service": {"name": "github", "api_base_url": BASE}
                },
            ),
            members=[],
        ),
    )
    yield service, state, requests
    await service.aclose()


async def read(service, **overrides):
    return await service.read(
        **{"resource": "dependency_alerts", "repo": REPO, **overrides}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"resource": "secrets"},
        {"resource": "https://evil.test"},
        {"repo": "https://github.com/a/b"},
        {"repo": "a/.."},
        {"repo": "a/b/../../secrets"},
        {"repo": "a/b?path=x"},
        {"repo": "a/b%2fsecret"},
        {"repo": "a/b#fragment"},
        {"identifier": "../secrets"},
        {"identifier": "0"},
        {"parameters": {"method": "PATCH"}},
        {"parameters": {"headers": {"Authorization": "x"}}},
        {"parameters": {"query": "mutation {}"}},
        {"parameters": {"after": "cursor"}},
        {"parameters": {"page_size": 0}},
        {"parameters": {"page_size": "30"}},
        {"identifier": "1", "parameters": {"state": "open"}},
        {"identifier": "1", "parameters": []},
        {"identifier": "1", "parameters": False},
        {"identifier": "1", "parameters": ""},
        {"identifier": "1", "continuation": "cursor"},
    ],
)
async def test_unapproved_requests_never_reach_network(reader, overrides):
    service, _, requests = reader
    with pytest.raises(RepositoryReadError):
        await read(service, **overrides)
    assert not requests


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
    service, state, requests = reader
    state["respond"] = lambda _: httpx.Response(
        200, json=[], headers={"Link": f'<{link}>; rel="next"'}
    )
    first = await read(service, parameters={"state": "resolved", "page_size": 1})
    assert len(requests) == 1
    assert first.continuation
    state["respond"] = lambda _: httpx.Response(200, json=[])
    second = await read(
        service,
        parameters={"state": "resolved", "page_size": 1},
        continuation=first.continuation,
    )
    assert second.items == [] and second.continuation is None
    assert len(requests) == 2
    assert all(
        request.method == "GET"
        and request.url.host == "github.example.test"
        and request.url.path == PATH
        for request in requests
    )
    assert dict(requests[1].url.params) == {
        "state": "fixed",
        "per_page": "1",
        "after": "cursor+1",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["page=2", "after=", "after=a&after=b", "after=" + "x" * 2049]
)
async def test_missing_ambiguous_or_oversized_link_cursor_is_rejected(reader, query):
    service, state, _ = reader
    state["respond"] = lambda _: httpx.Response(
        200,
        json=[],
        headers={
            "Link": f'<https://api.github.com/repositories/123/dependabot/alerts?{query}>; rel="next"'
        },
    )
    with pytest.raises(
        RepositoryReadError, match=t("integrations.github.read.continuation")
    ):
        await read(service)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "continuation",
    ["https://evil.test", "garbage", "e30=", "W10=", "bnVsbA==", "x" * 8193],
)
async def test_malformed_continuation_never_reaches_network(reader, continuation):
    service, _, requests = reader
    with pytest.raises(RepositoryReadError):
        await read(service, continuation=continuation)
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"repo": "other/repo"},
        {"parameters": {"state": "resolved"}},
        {"parameters": {"page_size": 1}},
        {"identifier": "1"},
        {"resource": "secrets"},
        {"api_base_url": "https://other.test/api/v3/"},
    ],
)
async def test_real_continuation_is_bound_to_original_request_and_api(
    reader, overrides
):
    service, state, requests = reader
    state["respond"] = lambda _: httpx.Response(
        200,
        json=[],
        headers={
            "Link": '<https://api.github.com/repositories/123/dependabot/alerts?after=cursor>; rel="next"'
        },
    )
    first = await read(service)
    overrides = dict(overrides)
    if "api_base_url" in overrides:
        await service.aclose()
        service.base_url = overrides.pop("api_base_url")
    with pytest.raises(RepositoryReadError):
        await read(service, continuation=first.continuation, **overrides)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "key"),
    [
        (401, {}, "authentication"),
        (403, {}, "forbidden"),
        (404, {}, "not_found"),
        (422, {}, "rejected"),
        (429, {}, "rate_limit"),
        (403, {"x-ratelimit-remaining": "0"}, "rate_limit"),
        (403, {"retry-after": "60"}, "rate_limit"),
        (500, {}, "http"),
        (302, {"location": "https://evil.test"}, "http"),
    ],
)
async def test_production_hook_errors_are_sanitized_and_redirects_not_followed(
    reader, status, headers, key, read_language
):
    service, state, requests = reader
    state["respond"] = lambda _: httpx.Response(
        status, text="secret-value", headers=headers
    )
    with pytest.raises(RepositoryReadError) as error:
        await read(service)
    expected = t(
        f"integrations.github.read.{key}", status=status, permission="Dependabot alerts"
    )
    if status == 404:
        expected += " " + t("integrations.github.read.alerts_disabled")
    assert str(error.value) == expected
    assert "secret-value" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"bad json", b"null", b"{}", b"[null]"])
async def test_bad_collection_shapes_fail(reader, body):
    service, state, _ = reader
    state["respond"] = lambda _: httpx.Response(200, content=body)
    with pytest.raises(
        RepositoryReadError, match=t("integrations.github.read.response")
    ):
        await read(service)


@pytest.mark.asyncio
async def test_raw_limit_is_enforced_by_authenticated_client(reader, monkeypatch):
    service, state, _ = reader
    monkeypatch.setattr(hosting, "MAX_PAGE_BYTES", 250)
    state["respond"] = lambda _: httpx.Response(200, content=b"x" * 251)
    with pytest.raises(
        RepositoryReadError, match=t("integrations.github.read.too_large")
    ):
        await read(service)


@pytest.mark.asyncio
async def test_detail_and_transport_failure(reader):
    service, state, requests = reader
    state["respond"] = lambda _: httpx.Response(
        200, json={"number": 42, "state": "open"}
    )
    result = await read(service, identifier="42")
    assert result.items[0].id == "42" and result.continuation is None
    assert requests[0].url.path == PATH + "/42" and not requests[0].url.query

    def fail(request):
        raise httpx.ConnectError("secret-value", request=request)

    state["respond"] = fail
    with pytest.raises(
        RepositoryReadError, match=t("integrations.github.read.transport")
    ):
        await read(service)
