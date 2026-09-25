"""A turn reaches its provider's API only through the gateway, and only with
the stand-in; the real token goes to the upstream and nowhere else."""

from __future__ import annotations

import asyncio
import gzip
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


@pytest.mark.asyncio
async def test_the_same_refused_route_is_logged_once_in_a_turn(
    running, caplog: pytest.LogCaptureFixture
) -> None:
    """A tool repeats a route its catalog does not name. The first refusal
    is how a missing route is found; the rest of the turn are the same fact,
    so the log says it once. A different route is said once of its own, a
    later turn (a new gateway) says it again, and every refusal is still 403
    that never reaches the upstream."""
    _gateway, upstream, tokens, guest = running
    caplog.set_level(logging.INFO, "guildbotics.intelligences.agent_environment")

    repeated = [
        await guest.post("/v1/oauth/token?secret=QUERY-592", content=b"BODY-592")
        for _ in range(3)
    ]
    other = await guest.get("/api/oauth/usage")

    assert [response.status_code for response in (*repeated, other)] == [
        403,
        403,
        403,
        403,
    ]
    assert upstream.requests == [] and tokens.asked == []
    assert caplog.messages.count("Gateway refused POST /v1/oauth/token") == 1
    assert caplog.messages.count("Gateway refused GET /api/oauth/usage") == 1
    for carried in ("QUERY-592", "BODY-592"):
        assert carried not in caplog.text

    later = _Upstream()
    gateway = CredentialGateway(
        BROKER, _Tokens(REAL), STAND_IN, transport=httpx.MockTransport(later)
    )
    await gateway.start()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers={"authorization": f"Bearer {gateway.stand_in}"},
        ) as again:
            response = await again.post("/v1/oauth/token")
    finally:
        await gateway.close()

    assert response.status_code == 403 and later.requests == []
    assert caplog.messages.count("Gateway refused POST /v1/oauth/token") == 2


@pytest.mark.asyncio
async def test_a_route_ending_in_a_star_forwards_what_is_under_it_only() -> None:
    """A path the guest spells to climb out -- its dots encoded, so that no
    client resolves them first -- is refused before it leaves the device."""
    upstream = _Upstream()
    broker = BROKER.model_copy(update={"routes": ("GET /agents/*",)})
    gateway = CredentialGateway(
        broker, _Tokens(REAL), STAND_IN, transport=httpx.MockTransport(upstream)
    )
    await gateway.start()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers={"authorization": f"Bearer {STAND_IN}"},
        ) as guest:
            forwarded = await guest.get("/agents/owner/repo?x=1")
            climbed = await guest.get("/agents/owner/%2e%2e/%2e%2e/v1/messages")
    finally:
        await gateway.close()

    assert forwarded.status_code == 200
    assert climbed.status_code == 403
    assert [str(r.url) for r in upstream.requests] == [
        "https://api.anthropic.com/agents/owner/repo?x=1"
    ]


_ELSEWHERE = CredentialBroker(
    format="t",
    access_token=("a",),
    refresh_token=("r",),
    expires_at=("e",),
    upstream="https://api.example.test",
    routes=("POST /v1/generate", "GET https://profile.example.test/me"),
    base_url_env=("API_URL",),
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


@pytest.mark.asyncio
async def test_a_login_refused_that_cannot_be_refreshed_is_answered_once() -> None:
    """A login that never refreshes is not sent again once refused: the
    guest is told to log in again."""
    upstream = _Upstream(_answer(401))

    class Revoked(_Tokens):
        async def __call__(self, refused: str | None) -> str:
            self.asked.append(refused)
            if refused is not None:
                raise CredentialUnavailableError("log in again")
            return REAL

    gateway = CredentialGateway(
        BROKER, Revoked(), STAND_IN, transport=httpx.MockTransport(upstream)
    )
    await gateway.start()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://127.0.0.1:{gateway.port}/v1/messages",
                headers={"authorization": f"Bearer {STAND_IN}"},
            )
    finally:
        await gateway.close()

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "log in again"
    assert len(upstream.requests) == 1


@pytest.mark.asyncio
async def test_an_answer_that_echoes_the_token_reaches_the_guest_masked(
    running,
) -> None:
    """The token never comes back into the turn, even from an upstream that
    echoes it -- in a header, or in a body where a chunk ends mid-token."""
    _gateway, upstream, _tokens, guest = running
    half = len(REAL) // 2
    body = [b'{"seen": "Bearer ' + REAL[:half].encode(), REAL[half:].encode() + b'"}']
    upstream.responses = [
        httpx.Response(
            200,
            headers={
                "x-echo": f"Bearer {REAL}",
                "content-length": str(sum(map(len, body))),
            },
            stream=_Chunks(body),
        )
    ]

    response = await guest.post("/v1/messages", headers={"accept-encoding": "gzip"})

    (sent,) = upstream.requests
    assert sent.headers["accept-encoding"] == "identity"
    assert REAL not in response.headers["x-echo"]
    assert REAL not in response.text
    assert response.text == '{"seen": "Bearer ' + "*" * len(REAL) + '"}'
    assert int(response.headers["content-length"]) == len(response.content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoding", ["gzip", "identity, gzip", "x-unknown", f"gzip-{REAL}"]
)
async def test_an_answer_encoded_despite_asking_for_none_is_not_handed_on(
    running, encoding: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The mask sees only the bytes as they come: in an encoded answer, the
    token would reach the turn once the tool decodes it. The log names what
    the guest asked for, and nothing the upstream said."""
    _gateway, upstream, _tokens, guest = running
    body = gzip.compress(b'{"seen": "Bearer ' + REAL.encode() + b'"}')
    upstream.responses = [_answer(200, body, **{"content-encoding": encoding})]

    with caplog.at_level(logging.INFO):
        response = await guest.post("/v1/messages")

    assert response.status_code == 502
    assert "content-encoding" not in response.headers
    assert "Gateway refused an encoded answer to POST /v1/messages" in caplog.messages
    assert REAL not in caplog.text and REAL not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        {"status": 200},
        {"status": 200, "content-encoding": f"gzip-{REAL}"},
        {"status": 302, "location": f"https://elsewhere.test/?t={REAL}"},
        {"status": 401},
        {"status": 500},
    ],
    ids=["answered", "encoded", "redirected", "refused", "failed"],
)
async def test_nothing_the_upstream_says_is_logged_as_it_said_it(
    answer: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """An upstream that echoes the token in whatever it answers -- the status
    line, a header, the body -- and however the gateway ends up answering the
    guest, puts it into neither GuildBotics' log nor the status line httpx logs
    at INFO. (httpcore's DEBUG trace, below any client, is not covered.)"""
    status = answer.pop("status")
    upstream = _Upstream(
        *(
            httpx.Response(
                status,
                headers={"x-echo": REAL, **answer},
                stream=httpx.ByteStream(REAL.encode()),
                extensions={"reason_phrase": f"Echo {REAL}".encode()},
            )
            for _ in range(2)
        )
    )
    gateway = CredentialGateway(
        BROKER, _Tokens(REAL, REAL), STAND_IN, transport=httpx.MockTransport(upstream)
    )
    await gateway.start()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers={"authorization": f"Bearer {STAND_IN}"},
        ) as guest:
            with caplog.at_level(logging.DEBUG):
                await guest.post("/v1/messages")
    finally:
        await gateway.close()

    assert upstream.requests
    assert "HTTP Request: POST" in caplog.text  # httpx logged the status line.
    assert REAL not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["", " Identity "])
async def test_an_answer_said_to_be_plain_is_handed_on_masked(
    running, encoding: str
) -> None:
    _gateway, upstream, _tokens, guest = running
    body = b'{"seen": "' + REAL.encode() + b'"}'
    upstream.responses = [_answer(200, body, **{"content-encoding": encoding})]

    response = await guest.post("/v1/messages")

    assert response.status_code == 200
    assert response.text == '{"seen": "' + "*" * len(REAL) + '"}'


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_neither_http2_nor_a_websocket_is_taken() -> None:
    """What the gateway does not carry is refused, not half-carried."""
    gateway = CredentialGateway(_ELSEWHERE, _Tokens(REAL), STAND_IN)
    await gateway.start()
    try:
        trust = ssl.create_default_context(cadata=gateway.ca_pem.decode())
        trust.set_alpn_protocols(["h2", "http/1.1"])
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", gateway.port, ssl=trust, server_hostname=GUEST_HOST_ALIAS
        )
        assert writer.get_extra_info("ssl_object").selected_alpn_protocol() == (
            "http/1.1"
        )
        writer.write(
            b"GET /v1/generate HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n"
            b"Authorization: Bearer " + STAND_IN.encode() + b"\r\n\r\n"
        )
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), 5)
        writer.close()
    finally:
        await gateway.close()

    assert status.startswith(b"HTTP/1.1 403"), status
