from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace

import httpx
import pytest

from guildbotics.integrations.github import async_client, github_utils
from guildbotics.runtime.port import ServiceError, ServiceNotFound
from guildbotics.utils.i18n_tool import t

_RESPONSE = "integrations.github.response"


def _github_app_auth(monkeypatch: pytest.MonkeyPatch) -> github_utils.GitHubAppAuth:
    auth = object.__new__(github_utils.GitHubAppAuth)
    auth.app_id = "1"
    auth.installation_id = "2"
    auth.person_id = "alice"
    auth._token = "expired"
    auth._expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    auth._leeway = dt.timedelta(seconds=120)
    monkeypatch.setattr(
        auth,
        "_build_refresh_request",
        lambda request: httpx.Request(
            "POST",
            request.url.copy_with(path="/app/installations/2/access_tokens"),
            extensions=request.extensions,
        ),
    )
    return auth


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.count = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", ["initial", "unauthorized"])
@pytest.mark.parametrize("status", [201, 500])
async def test_app_refresh_inherits_request_bound_before_body_buffering(
    monkeypatch, refresh, status
):
    auth = _github_app_auth(monkeypatch)
    auth._private_key = "unused"
    monkeypatch.setattr(github_utils, "_build_github_app_jwt", lambda *_: "test-jwt")
    auth._build_refresh_request = (
        github_utils.GitHubAppAuth._build_refresh_request.__get__(auth)
    )
    if refresh == "initial":
        auth._expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    stream = CountingStream([b"x" * 100] * 100)
    client = async_client.get_async_client("https://api.github.com", auth, "acme")
    client._transport = httpx.MockTransport(
        lambda request: (
            httpx.Response(status, stream=stream)
            if request.method == "POST"
            else httpx.Response(401, text="expired")
        )
    )
    client._mounts = {}
    async with client:
        with pytest.raises(ServiceError, match=re.escape(t(f"{_RESPONSE}.too_large"))):
            await client.get(
                "/repos/acme/repo/dependabot/alerts",
                extensions={"max_response_bytes": 150},
            )
    assert stream.count == 2 and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["app", "token"])
@pytest.mark.parametrize("status", [200, 401, 403, 500])
async def test_bounded_client_stops_before_auth_or_error_hooks_buffer_body(
    monkeypatch, kind, status
):
    auth = (
        _github_app_auth(monkeypatch)
        if kind == "app"
        else github_utils.GitHubTokenAuth("token")
    )
    stream = CountingStream([b"x" * 100] * 100)
    client = async_client.get_async_client("https://api.github.com", auth, "acme")
    client._transport = httpx.MockTransport(
        lambda _: httpx.Response(status, stream=stream)
    )
    client._mounts = {}
    async with client:
        with pytest.raises(ServiceError, match=re.escape(t(f"{_RESPONSE}.too_large"))):
            await client.get(
                "/repos/acme/repo/dependabot/alerts",
                extensions={"max_response_bytes": 150},
            )
    assert stream.count == 2
    assert stream.closed


@pytest.mark.asyncio
async def test_bounded_app_client_refreshes_and_retries_without_losing_body(
    monkeypatch,
):
    auth = _github_app_auth(monkeypatch)
    seen = []
    bodies = [b"expired", b'{"token":"new","expires_at":"2099-01-01T00:00:00Z"}', b"[]"]
    streams = [CountingStream([body]) for body in bodies]

    def respond(request):
        seen.append((request.method, request.headers.get("Authorization")))
        i = len(seen) - 1
        return httpx.Response([401, 201, 200][i], stream=streams[i])

    client = async_client.get_async_client("https://api.github.com", auth, "acme")
    client._transport = httpx.MockTransport(respond)
    client._mounts = {}
    async with client:
        result = await client.get(
            "/repos/acme/repo/dependabot/alerts", extensions={"max_response_bytes": 150}
        )
        assert result.json() == []
    assert seen == [("GET", "token expired"), ("POST", None), ("GET", "token new")]
    assert all(stream.closed and stream.count == 1 for stream in streams)


@pytest.mark.asyncio
async def test_bounded_client_limits_decoded_compressed_body():
    import gzip

    stream = CountingStream([gzip.compress(b"x" * 1000)])
    client = async_client.get_async_client(
        "https://api.github.com",
        github_utils.GitHubTokenAuth("token"),
        "acme",
    )
    client._transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, stream=stream, headers={"content-encoding": "gzip"}
        )
    )
    client._mounts = {}
    async with client:
        with pytest.raises(ServiceError, match=re.escape(t(f"{_RESPONSE}.too_large"))):
            await client.get(
                "/repos/acme/repo/dependabot/alerts",
                extensions={"max_response_bytes": 150},
            )
    assert stream.closed


def _client(auth: httpx.Auth, respond) -> httpx.AsyncClient:
    """A member's client of api.github.com whose transport is ``respond``."""
    client = async_client.get_async_client("https://api.github.com", auth, "acme")
    client._transport = httpx.MockTransport(respond)
    client._mounts = {}
    return client


@pytest.fixture
def recorded(monkeypatch) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setattr(
        async_client, "record_correlated_event", lambda **kwargs: events.append(kwargs)
    )
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "kind", "message"),
    [
        (401, {}, async_client.GitHubResponseError, t(f"{_RESPONSE}.authentication")),
        (403, {}, async_client.GitHubResponseError, t(f"{_RESPONSE}.forbidden")),
        (
            403,
            {"x-ratelimit-remaining": "0"},
            async_client.GitHubResponseError,
            t(f"{_RESPONSE}.rate_limit"),
        ),
        (404, {}, async_client.GitHubNotFound, t(f"{_RESPONSE}.not_found")),
        (422, {}, async_client.GitHubResponseError, t(f"{_RESPONSE}.rejected")),
        (429, {}, async_client.GitHubResponseError, t(f"{_RESPONSE}.rate_limit")),
        (502, {}, async_client.GitHubResponseError, t(f"{_RESPONSE}.http", status=502)),
    ],
)
async def test_an_error_answer_is_a_service_error_told_from_its_status_alone(
    recorded, status, headers, kind, message
):
    """#787: the client's response hook turns every error answer into the
    ports' failure, so no provider sees an error response; what GitHub wrote
    in it never reaches the message."""
    client = _client(
        github_utils.GitHubTokenAuth("token"),
        lambda _: httpx.Response(
            status, headers=headers, json={"message": "SECRET-BODY ghp_leak"}
        ),
    )
    async with client:
        with pytest.raises(ServiceError) as raised:
            await client.get("/repos/acme/demo/issues/1")

    assert type(raised.value) is kind
    assert raised.value.status == status
    assert str(raised.value) == message
    assert "SECRET-BODY" not in str(raised.value)
    assert isinstance(raised.value, ServiceNotFound) == (status == 404)
    assert [event["event_type"] for event in recorded] == (
        ["credential.failed"] if status == 401 else []
    )


@pytest.mark.asyncio
async def test_a_transport_failure_is_a_service_error(recorded):
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to SECRET-HOST", request=request)

    async with _client(github_utils.GitHubTokenAuth("token"), respond) as client:
        with pytest.raises(ServiceError) as raised:
            await client.get("/repos/acme/demo/issues/1")

    assert type(raised.value) is ServiceError
    assert str(raised.value) == t(f"{_RESPONSE}.transport")
    assert recorded == []


@pytest.mark.asyncio
async def test_an_unauthorized_answer_records_the_refused_credential(recorded):
    auth = github_utils.GitHubTokenAuth("expired", person_id="alice")
    respond = lambda _: httpx.Response(401, text="unauthorized")  # noqa: E731

    async with _client(auth, respond) as client:
        with pytest.raises(async_client.GitHubResponseError):
            await client.get("/rate_limit")

    assert len(recorded) == 1
    assert recorded[0]["event_type"] == "credential.failed"
    assert recorded[0]["person_id"] == "alice"
    assert recorded[0]["attributes"] == {
        "credential.provider": "github",
        "error.category": "authentication",
    }
    assert recorded[0]["payload"] == {"provider": "github", "code": "unauthorized"}


@pytest.mark.asyncio
async def test_an_app_retry_refused_again_is_an_authentication_failure_recorded_once(
    monkeypatch, recorded
):
    """The app's first 401 is left to its auth flow, which renews the token;
    a 401 to the retry is the answer, recorded once."""
    auth = _github_app_auth(monkeypatch)
    statuses = iter([401, 201, 401])

    def respond(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        if status == 201:
            return httpx.Response(
                201, json={"token": "new", "expires_at": "2099-01-01T00:00:00Z"}
            )
        return httpx.Response(status, text="expired")

    async with _client(auth, respond) as client:
        with pytest.raises(async_client.GitHubResponseError) as raised:
            await client.get("/rate_limit")

    assert raised.value.status == 401
    assert str(raised.value) == t(f"{_RESPONSE}.authentication")
    assert [event["person_id"] for event in recorded] == ["alice"]


def test_github_app_refresh_success_does_not_record_failure(monkeypatch) -> None:
    auth = _github_app_auth(monkeypatch)
    recorded: list[dict[str, str]] = []
    monkeypatch.setattr(
        github_utils,
        "record_github_auth_failure",
        lambda **kwargs: recorded.append(kwargs),
    )
    request = httpx.Request("GET", "https://api.github.com/rate_limit")
    flow = auth.auth_flow(request)

    first_request = next(flow)
    refresh_request = flow.send(httpx.Response(401, request=first_request))
    retry_request = flow.send(
        httpx.Response(
            201,
            request=refresh_request,
            json={"token": "fresh", "expires_at": "2026-07-12T00:00:00Z"},
        )
    )
    with pytest.raises(StopIteration):
        flow.send(httpx.Response(200, request=retry_request))

    assert retry_request.headers["Authorization"] == "token fresh"
    assert recorded == []


def test_github_app_retry_unauthorized_records_failure_once(monkeypatch) -> None:
    auth = _github_app_auth(monkeypatch)
    recorded: list[dict[str, str]] = []
    monkeypatch.setattr(
        github_utils,
        "record_github_auth_failure",
        lambda **kwargs: recorded.append(kwargs),
    )
    request = httpx.Request("GET", "https://api.github.com/rate_limit")
    flow = auth.auth_flow(request)

    first_request = next(flow)
    refresh_request = flow.send(httpx.Response(401, request=first_request))
    retry_request = flow.send(
        httpx.Response(
            201,
            request=refresh_request,
            json={"token": "fresh", "expires_at": "2026-07-12T00:00:00Z"},
        )
    )
    with pytest.raises(httpx.HTTPStatusError):
        flow.send(httpx.Response(401, request=retry_request))

    assert recorded == [{"person_id": "alice"}]


@pytest.mark.asyncio
async def test_invalid_github_app_key_records_credential_failure(monkeypatch) -> None:
    recorded = []
    person = SimpleNamespace(
        person_id="alice",
        get_account_info=lambda key: {
            "github_app_id": "1",
            "github_installation_id": "2",
        }[key],
    )
    monkeypatch.setattr(
        github_utils,
        "get_github_account_type",
        lambda _person: github_utils.GitHubAppAuth.GITHUB_APPS,
    )
    monkeypatch.setattr(
        github_utils, "get_person_private_key_pem", lambda _person: b"invalid"
    )
    monkeypatch.setattr(
        github_utils,
        "record_github_auth_failure",
        lambda **kwargs: recorded.append(kwargs),
    )

    with pytest.raises(ValueError):
        await github_utils.create_github_client(
            person, "https://api.github.com", "acme"
        )

    assert recorded == [{"person_id": "alice", "code": "invalid_app_credential"}]
