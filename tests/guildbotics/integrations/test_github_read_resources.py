"""The generic read boundary admits data, never caller-selected HTTP requests."""

import base64
import json

import httpx
import pytest

from guildbotics.integrations.github import read_resources as reads
from guildbotics.integrations.github.async_client import get_async_client
from guildbotics.integrations.github.github_utils import GitHubTokenAuth
from guildbotics.utils.i18n_tool import t

REPO = "GuildBotics/GuildBotics"
PATH = f"/repos/{REPO}/dependabot/alerts"


def prepared(**overrides):
    return reads.prepare_read(
        **{
            "resource": "dependabot-alerts",
            "repo": REPO,
            "identifier": "",
            "parameters": "{}",
            **overrides,
        }
    )


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
        {"identifier": "42"},
        {"resource": "dependabot-alert", "identifier": "../secrets"},
        {"resource": "dependabot-alert", "identifier": "0"},
        {"resource": "dependabot-alert", "identifier": ""},
        {"parameters": '{"method":"PATCH"}'},
        {"parameters": '{"headers":{"Authorization":"x"}}'},
        {"parameters": '{"query":"mutation {}"}'},
        {"parameters": '{"after":"https://evil.test"}'},
        {"parameters": '{"state":"all"}'},
        {"parameters": '{"per_page":101}'},
        {"parameters": '{"per_page":0}'},
        {"parameters": '{"per_page":true}'},
        {"parameters": '{"per_page":"30"}'},
        {"parameters": "[]"},
        {"parameters": "invalid"},
        {
            "resource": "dependabot-alert",
            "identifier": "1",
            "parameters": '{"state":"open"}',
        },
    ],
)
def test_unapproved_requests_are_rejected(overrides):
    with pytest.raises(reads.GitHubReadError):
        prepared(**overrides)


@pytest.mark.asyncio
async def test_pages_are_explicit_and_keep_conditions_and_api_base():
    requests = []
    base = "https://github.example.test/api/v3/"
    link = (
        f"{base}repos/{REPO}/dependabot/alerts?state=fixed&per_page=1&after=cursor%2B1"
    )

    def respond(request):
        requests.append(request)
        if request.url.params.get("after"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200, json=[{"number": 1}], headers={"Link": f'<{link}>; rel="next"'}
        )

    definition, request = prepared(parameters='{"state":"fixed","per_page":1}')
    async with httpx.AsyncClient(
        base_url=base, transport=httpx.MockTransport(respond)
    ) as client:
        first = await reads.read_page(client, definition, request)
        assert len(requests) == 1
        second = await reads.read_page(
            client, definition, request, first["continuation"]
        )
    assert second["data"] == []
    assert second["continuation"] is None
    assert requests[1].url.path == "/api/v3" + PATH
    assert dict(requests[1].url.params) == {
        "state": "fixed",
        "per_page": "1",
        "after": "cursor+1",
    }
    assert all(request.method == "GET" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link",
    [
        f"https://evil.test{PATH}?state=open&per_page=30&after=a",
        f"https://api.github.com{PATH}/1?state=open&per_page=30&after=a",
        f"https://api.github.com{PATH}?state=fixed&per_page=30&after=a",
        f"https://api.github.com{PATH}?state=open&per_page=30&after=a&after=b",
        f"https://api.github.com{PATH}?state=open&per_page=30&after=a&method=PATCH",
        f"https://api.github.com{PATH}?state=open&per_page=30",
        f"https://user:password@api.github.com{PATH}?state=open&per_page=30&after=a",
        f"https://api.github.com{PATH}?state=open&per_page=30&after=a#fragment",
    ],
)
async def test_next_links_cannot_change_destination_or_conditions(link):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=[], headers={"Link": f'<{link}>; rel="next"'})

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(
            reads.GitHubReadError, match=t("integrations.github.read.continuation")
        ):
            await reads.read_page(client, *prepared())
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "continuation", ["https://evil.test", "garbage", "e30=", "W10=", "bnVsbA=="]
)
async def test_malformed_continuation_never_reaches_network(continuation):
    async with httpx.AsyncClient(base_url="https://api.github.com") as client:
        with pytest.raises(reads.GitHubReadError):
            await reads.read_page(client, *prepared(), continuation)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"repo": "other/repo"},
        {"parameters": '{"state":"fixed"}'},
        {"parameters": '{"per_page":1}'},
        {"resource": "dependabot-alert", "identifier": "1"},
    ],
)
async def test_continuations_are_bound_to_the_original_request(overrides):
    _, request = prepared()
    request["api_base_url"] = "https://api.github.com"
    token = base64.urlsafe_b64encode(
        json.dumps({"request": request, "after": "cursor"}).encode()
    ).decode()
    async with httpx.AsyncClient(base_url="https://api.github.com") as client:
        with pytest.raises(reads.GitHubReadError):
            await reads.read_page(client, *prepared(**overrides), token)


@pytest.mark.asyncio
async def test_real_continuation_cannot_move_to_another_api():
    def respond(_):
        return httpx.Response(
            200,
            json=[],
            headers={
                "Link": f'<https://api.github.com{PATH}?state=open&per_page=30&after=cursor>; rel="next"'
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(respond)
    ) as client:
        page = await reads.read_page(client, *prepared())
    calls = []
    async with httpx.AsyncClient(
        base_url="https://other.test/api/v3/",
        transport=httpx.MockTransport(lambda request: calls.append(request)),
    ) as client:
        with pytest.raises(reads.GitHubReadError):
            await reads.read_page(client, *prepared(), page["continuation"])
    assert calls == []


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
async def test_errors_do_not_return_upstream_bodies_or_credentials(
    status, headers, key
):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, text="secret-value", headers=headers)

    # Use the production response hook too: it raises before stream() yields.
    client = get_async_client(
        "https://api.github.com", GitHubTokenAuth("secret-value"), "GuildBotics"
    )
    client._transport = httpx.MockTransport(respond)
    # Avoid environment proxy mounts in this hermetic test.
    client._mounts = {}
    async with client:
        with pytest.raises(reads.GitHubReadError) as error:
            await reads.read_page(client, *prepared())
    assert str(error.value) == t(f"integrations.github.read.{key}", status=status)
    assert "secret-value" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"bad json", b"null", b"{}", b"[null]"])
async def test_bad_collection_shapes_are_failures(body):
    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
    ) as client:
        with pytest.raises(reads.GitHubReadError):
            await reads.read_page(client, *prepared())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [b'[{"text":"' + b"x" * 300 + b'"}]', b'[{"text":"' + "あ".encode() * 35 + b'"}]'],
)
async def test_raw_and_serialized_page_limits_are_errors(monkeypatch, body):
    monkeypatch.setattr(reads, "MAX_PAGE_BYTES", 250)
    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
    ) as client:
        with pytest.raises(reads.GitHubReadError) as error:
            await reads.read_page(client, *prepared())
    assert str(error.value) == t("integrations.github.read.too_large")


@pytest.mark.asyncio
async def test_detail_and_transport_failure():
    def respond(request):
        assert request.url.path == PATH + "/42"
        assert not request.url.query
        return httpx.Response(200, json={"number": 42})

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(respond)
    ) as client:
        result = await reads.read_page(
            client, *prepared(resource="dependabot-alert", identifier="42")
        )
    assert result["data"] == {"number": 42}
    assert result["continuation"] is None

    def fail(request):
        raise httpx.ConnectError("secret-value", request=request)

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(fail)
    ) as client:
        with pytest.raises(reads.GitHubReadError) as error:
            await reads.read_page(client, *prepared())
    assert str(error.value) == t("integrations.github.read.transport")
