"""GitHub reads keep their approved destination and bounded authenticated client."""

import httpx
import pytest
import pytest_asyncio

from guildbotics.entities import Person, Project, Team
from guildbotics.integrations.code_hosting_service import RepositoryReadError
from guildbotics.integrations.github import code_hosting_service as hosting
from guildbotics.integrations.github.async_client import get_async_client
from guildbotics.integrations.github.github_utils import GitHubTokenAuth
from guildbotics.utils.i18n_tool import t

REPO = "GuildBotics/GuildBotics"
BASE = "https://github.example.test/api/v3/"
PATH = f"/api/v3/repos/{REPO}/dependabot/alerts"


@pytest_asyncio.fixture
async def reader(monkeypatch):
    requests = []
    state = {"respond": lambda _: httpx.Response(200, json=[])}

    def respond(request):
        requests.append(request)
        return state["respond"](request)

    async def create(_person, base_url, owner, *, max_response_bytes):
        client = get_async_client(
            base_url,
            GitHubTokenAuth("secret-value"),
            owner,
            max_response_bytes=max_response_bytes,
        )
        client._transport = httpx.MockTransport(respond)
        client._mounts = {}
        return client

    monkeypatch.setattr(hosting, "create_github_client", create)
    service = hosting.GitHubCodeHostingService(
        Person(person_id="aiko", name="Aiko"),
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
    reader, status, headers, key
):
    service, state, requests = reader
    state["respond"] = lambda _: httpx.Response(
        status, text="secret-value", headers=headers
    )
    with pytest.raises(RepositoryReadError) as error:
        await read(service)
    assert str(error.value) == t(f"integrations.github.read.{key}", status=status)
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
