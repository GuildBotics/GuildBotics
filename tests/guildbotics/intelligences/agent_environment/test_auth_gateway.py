"""A turn reaches its provider's API only through the gateway, and only with
the stand-in; the real token goes to the upstream and nowhere else."""

from __future__ import annotations

import logging
import ssl
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from guildbotics.intelligences.agent_environment.auth_gateway import (
    CredentialGateway,
    CredentialUnavailableError,
)
from guildbotics.intelligences.agent_environment.spec import GUEST_HOST_ALIAS
from guildbotics.intelligences.cli_agents import CredentialBroker, cli_agent_info

BROKER = cli_agent_info("claude").provision.credential_broker
assert BROKER is not None
REAL = "REAL-SYNTHETIC-TOKEN-459"
STAND_IN = "guildbotics-stand-in-SYNTHETIC-459"


class _Tokens:
    """The login as the gateway sees it: a token, and a refresh on refusal."""

    def __init__(self, *tokens: str, error: Exception | None = None) -> None:
        self.tokens = list(tokens)
        self.asked: list[str | None] = []
        self.error = error

    async def __call__(self, refused: str | None) -> str:
        self.asked.append(refused)
        if self.error is not None:
            raise self.error
        if refused is not None:
            self.tokens.pop(0)
        return self.tokens[0]


def _answer(status: int, body: bytes = b"", **headers: str) -> httpx.Response:
    """An upstream answer that is streamed, as one off the network is."""
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


class _Upstream:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if self.responses else _answer(200)


@pytest_asyncio.fixture
async def running() -> AsyncIterator[
    tuple[CredentialGateway, _Upstream, _Tokens, httpx.AsyncClient]
]:
    upstream = _Upstream()
    tokens = _Tokens(REAL, "REFRESHED-SYNTHETIC-459")
    gateway = CredentialGateway(
        BROKER, tokens, STAND_IN, transport=httpx.MockTransport(upstream)
    )
    await gateway.start()
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{gateway.port}",
        headers={"authorization": f"Bearer {gateway.stand_in}"},
    ) as guest:
        yield gateway, upstream, tokens, guest
    await gateway.close()


@pytest.mark.asyncio
async def test_a_route_with_the_stand_in_reaches_the_upstream_with_the_real_token(
    running,
) -> None:
    gateway, upstream, _tokens, guest = running

    response = await guest.post(
        "/v1/messages?beta=true",
        content=b'{"model": "m"}',
        headers={
            "anthropic-beta": "oauth-2025-04-20",
            "x-api-key": "guest-key",
            "cookie": "guest=1",
            "host": "attacker.example",
        },
    )

    assert response.status_code == 200
    (sent,) = upstream.requests
    assert str(sent.url) == f"{BROKER.upstream}/v1/messages?beta=true"
    assert sent.headers["authorization"] == f"Bearer {REAL}"
    assert sent.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert sent.headers["host"] == "api.anthropic.com"
    assert "x-api-key" not in sent.headers and "cookie" not in sent.headers
    assert sent.content == b'{"model": "m"}'
    assert gateway.stand_in not in str(sent.headers)


@pytest.mark.asyncio
async def test_the_guest_is_told_where_the_gateway_is(running) -> None:
    gateway, *_ = running

    assert gateway.turn_environment() == {
        "ANTHROPIC_BASE_URL": f"http://{GUEST_HOST_ALIAS}:{gateway.port}",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    ["", "Bearer wrong", f"Bearer {REAL}", "Basic x"],
)
async def test_anything_but_the_stand_in_is_refused_before_the_upstream(
    running, authorization: str
) -> None:
    _gateway, upstream, tokens, guest = running

    response = await guest.post(
        "/v1/messages", headers={"authorization": authorization}
    )

    assert response.status_code == 401
    assert upstream.requests == [] and tokens.asked == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/messages"),
        ("POST", "/v1/oauth/token"),
        ("POST", "/api/oauth/usage"),
        ("POST", "/v1/messages/../oauth/token"),
        ("POST", "/v1/messages/batches"),
    ],
)
async def test_only_the_named_routes_are_forwarded(
    running, method: str, path: str
) -> None:
    _gateway, upstream, tokens, guest = running

    response = await guest.request(method, path)

    assert response.status_code == 403
    assert upstream.requests == [] and tokens.asked == []


@pytest.mark.asyncio
async def test_a_refused_token_is_refreshed_once_and_the_request_sent_again(
    running,
) -> None:
    _gateway, upstream, tokens, guest = running
    upstream.responses = [_answer(401), _answer(401)]

    response = await guest.post("/v1/messages", content=b"body")

    assert response.status_code == 401
    assert tokens.asked == [None, REAL]
    assert [r.headers["authorization"] for r in upstream.requests] == [
        f"Bearer {REAL}",
        "Bearer REFRESHED-SYNTHETIC-459",
    ]
    assert [r.content for r in upstream.requests] == [b"body", b"body"]


@pytest.mark.asyncio
async def test_a_redirect_is_handed_back_not_followed(running) -> None:
    _gateway, upstream, _tokens, guest = running
    upstream.responses = [_answer(307, location="https://elsewhere.example/x")]

    response = await guest.post("/v1/messages")

    assert response.status_code == 307
    assert response.headers["location"] == "https://elsewhere.example/x"
    assert len(upstream.requests) == 1


@pytest.mark.asyncio
async def test_a_streamed_answer_passes_through(running) -> None:
    _gateway, upstream, _tokens, guest = running
    events = b"event: message_start\ndata: {}\n\nevent: message_stop\ndata: {}\n\n"
    upstream.responses = [_answer(200, events, **{"content-type": "text/event-stream"})]

    async with guest.stream("POST", "/v1/messages") as response:
        body = b"".join([chunk async for chunk in response.aiter_raw()])

    assert response.headers["content-type"] == "text/event-stream"
    assert body == events


@pytest.mark.asyncio
async def test_a_login_that_cannot_give_a_token_answers_why(running) -> None:
    _gateway, upstream, tokens, guest = running
    tokens.error = CredentialUnavailableError("log in again")

    response = await guest.post("/v1/messages")

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "log in again"
    assert upstream.requests == []


@pytest.mark.asyncio
async def test_an_unreachable_upstream_is_a_bad_gateway() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    gateway = CredentialGateway(
        BROKER, _Tokens(REAL), STAND_IN, transport=httpx.MockTransport(unreachable)
    )
    await gateway.start()
    try:
        async with httpx.AsyncClient() as guest:
            response = await guest.post(
                f"http://127.0.0.1:{gateway.port}/v1/messages",
                headers={"authorization": f"Bearer {gateway.stand_in}"},
            )
        assert response.status_code == 502
        assert REAL not in response.text
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_after_the_turn_the_stand_in_opens_nothing() -> None:
    upstream = _Upstream()
    gateway = CredentialGateway(
        BROKER, _Tokens(REAL), STAND_IN, transport=httpx.MockTransport(upstream)
    )
    await gateway.start()
    port, stand_in = gateway.port, gateway.stand_in

    await gateway.close()
    await gateway.close()

    async with httpx.AsyncClient() as guest:
        with pytest.raises(httpx.ConnectError):
            await guest.post(
                f"http://127.0.0.1:{port}/v1/messages",
                headers={"authorization": f"Bearer {stand_in}"},
            )
    assert upstream.requests == []


@pytest.mark.asyncio
async def test_a_gateway_takes_its_own_turns_stand_in_only() -> None:
    first = CredentialGateway(BROKER, _Tokens(REAL), STAND_IN)
    second = CredentialGateway(BROKER, _Tokens(REAL), STAND_IN + "-other")
    await first.start()
    await second.start()
    try:
        async with httpx.AsyncClient() as guest:
            response = await guest.post(
                f"http://127.0.0.1:{second.port}/v1/messages",
                headers={"authorization": f"Bearer {first.stand_in}"},
            )
        assert response.status_code == 401
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_a_request_the_guest_abandons_is_never_forwarded() -> None:
    """A partial body is not sent on with the real token."""
    upstream = _Upstream()
    tokens = _Tokens(REAL)
    gateway = CredentialGateway(
        BROKER, tokens, STAND_IN, transport=httpx.MockTransport(upstream)
    )
    await gateway.start()
    messages = iter(
        [
            {"type": "http.request", "body": b'{"partial', "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    try:
        await gateway(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/messages",
                "query_string": b"",
                "headers": [(b"authorization", f"Bearer {gateway.stand_in}".encode())],
            },
            receive,
            send,
        )
    finally:
        await gateway.close()

    assert upstream.requests == [] and tokens.asked == [] and sent == []


@pytest.mark.asyncio
async def test_a_tool_whose_api_lives_under_a_path_is_told_the_path_too() -> None:
    grok = cli_agent_info("grok").provision.credential_broker
    assert grok is not None
    gateway = CredentialGateway(grok, _Tokens(REAL), STAND_IN)
    await gateway.start()
    try:
        assert gateway.turn_environment() == {
            "GROK_CLI_CHAT_PROXY_BASE_URL": (
                f"http://{GUEST_HOST_ALIAS}:{gateway.port}/v1"
            )
        }
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_a_refused_route_is_logged_without_what_it_carried(
    running, caplog: pytest.LogCaptureFixture
) -> None:
    """The log says what a tool asked for that its catalog does not name --
    how a missing route is found -- and never the credential or query."""
    gateway, _upstream, _tokens, guest = running
    caplog.set_level(logging.INFO, "guildbotics.intelligences.agent_environment")

    await guest.post("/v1/oauth/token?secret=QUERY-459", content=b"BODY-459")

    assert "POST /v1/oauth/token" in caplog.text
    for carried in (gateway.stand_in, "QUERY-459", "BODY-459"):
        assert carried not in caplog.text


_ELSEWHERE = CredentialBroker(
    format="t",
    access_token=("a",),
    refresh_token=("r",),
    expires_at=("e",),
    upstream="https://api.example.test",
    routes=("POST /v1/generate", "GET https://profile.example.test/me"),
    base_url_env="API_URL",
    tls=True,
    relayed_hosts=("profile.example.test",),
    refresh=("tool", "refresh"),
)


@pytest.mark.asyncio
async def test_a_route_that_names_its_origin_is_forwarded_there() -> None:
    upstream = _Upstream()
    gateway = CredentialGateway(
        _ELSEWHERE, _Tokens(REAL), STAND_IN, transport=httpx.MockTransport(upstream)
    )
    await gateway.start()
    try:
        trust = ssl.create_default_context(cadata=gateway.ca_pem.decode())
        async with httpx.AsyncClient(
            base_url=f"https://127.0.0.1:{gateway.port}",
            headers={"authorization": f"Bearer {STAND_IN}"},
            verify=trust,
            # A connection of its own per name, so each is its own handshake.
            limits=httpx.Limits(max_keepalive_connections=0),
        ) as guest:
            for method, path, name in (
                ("POST", "/v1/generate", GUEST_HOST_ALIAS),
                ("GET", "/me", "profile.example.test"),
            ):
                response = await guest.request(
                    method, path, extensions={"sni_hostname": name}
                )
                assert response.status_code == 200
    finally:
        await gateway.close()

    assert [str(r.url) for r in upstream.requests] == [
        "https://api.example.test/v1/generate",
        "https://profile.example.test/me",
    ]


@pytest.mark.asyncio
async def test_a_tool_that_takes_https_only_trusts_the_turns_ca_for_its_names_only() -> (
    None
):
    gateway = CredentialGateway(_ELSEWHERE, _Tokens(REAL), STAND_IN)
    await gateway.start()
    try:
        assert gateway.turn_environment() == {
            "API_URL": f"https://{GUEST_HOST_ALIAS}:{gateway.port}"
        }
        trust = ssl.create_default_context(cadata=gateway.ca_pem.decode())
        async with httpx.AsyncClient(verify=trust) as guest:
            with pytest.raises(httpx.ConnectError):
                await guest.get(
                    f"https://127.0.0.1:{gateway.port}/me",
                    extensions={"sni_hostname": "evil.example.test"},
                )
        async with httpx.AsyncClient() as untrusting:  # The system's CAs alone.
            with pytest.raises(httpx.ConnectError):
                await untrusting.get(
                    f"https://127.0.0.1:{gateway.port}/me",
                    extensions={"sni_hostname": GUEST_HOST_ALIAS},
                )
    finally:
        await gateway.close()
