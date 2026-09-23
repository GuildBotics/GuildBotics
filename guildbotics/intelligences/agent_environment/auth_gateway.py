"""The gateway a turn reaches its provider's API through, outside the microVM.

A turn of a tool whose login is brokered holds a stand-in token and nothing
else of the login. The tool is pointed at this gateway, which runs in the
GuildBotics process on a loopback port only this turn's microVM is let
through to, for as long as the turn runs. A request is forwarded only when
it carries this turn's stand-in and is one of the routes the tool's catalog
entry names; it then goes to the tool's one upstream origin, with the
stand-in replaced by the real access token in the ``Authorization`` header
and nowhere else. Redirects are handed back, never followed, so the token is
never sent anywhere but the upstream. The request's own host, credentials,
and hop-by-hop headers are dropped.

When the upstream refuses the token, the login is refreshed once and the
request sent again. The token itself lives in this process's memory: where
it comes from, and how it is refreshed, is the login's business
(:class:`TokenSource`).
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from guildbotics.intelligences.agent_environment.spec import GUEST_HOST_ALIAS
from guildbotics.intelligences.cli_agents import CredentialBroker
from guildbotics.utils.loopback_server import LoopbackServer

#: Hands out the access token to send; given the token the upstream just
#: refused, it refreshes the login unless that was done already.
TokenSource = Callable[[str | None], Awaitable[str]]

_MAX_REQUEST_BYTES = 64 * 1024 * 1024
_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=30.0)
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
#: What of the guest's request never reaches the upstream: the name it used
#: for the gateway, the credentials it holds, and a length the forwarded
#: body sets again.
_DROPPED_REQUEST_HEADERS = _HOP_BY_HOP | {
    "host",
    "authorization",
    "x-api-key",
    "cookie",
    "content-length",
}


class CredentialUnavailableError(RuntimeError):
    """The login cannot give a token; the message says what to do."""


class CredentialGateway:
    """One turn's gateway to its tool's API."""

    def __init__(
        self,
        broker: CredentialBroker,
        tokens: TokenSource,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._broker = broker
        self._tokens = tokens
        self._routes = frozenset(tuple(route.split(" ", 1)) for route in broker.routes)
        self._authorization = ""
        self.stand_in = ""
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._server: LoopbackServer | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("The credential gateway is not running.")
        return self._server.port

    def turn_environment(self) -> dict[str, str]:
        """What the tool inside the turn is told: where its API is."""
        return {
            self._broker.base_url_env: f"http://{GUEST_HOST_ALIAS}:{self.port}",
            **dict(self._broker.turn_environment),
        }

    async def start(self) -> None:
        """Mint this turn's stand-in and start accepting it."""
        self.stand_in = "guildbotics-stand-in-" + secrets.token_urlsafe(32)
        self._authorization = f"Bearer {self.stand_in}"
        self._client = httpx.AsyncClient(
            transport=self._transport, follow_redirects=False, timeout=_TIMEOUT
        )
        try:
            self._server = await LoopbackServer.start(lambda _port: self)
        except BaseException:
            await self._client.aclose()
            raise

    async def close(self) -> None:
        """Refuse the stand-in from now on and stop the gateway; idempotent."""
        self._authorization = ""
        server, self._server = self._server, None
        if server is None:
            return
        try:
            await server.stop()
        finally:
            if self._client is not None:
                await self._client.aclose()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while (await receive())["type"] != "lifespan.shutdown":
                await send({"type": "lifespan.startup.complete"})
            await send({"type": "lifespan.shutdown.complete"})
            return
        if scope["type"] != "http":
            return
        headers = [
            (name.decode("latin-1").lower(), value.decode("latin-1"))
            for name, value in scope["headers"]
        ]
        presented = next((v for k, v in headers if k == "authorization"), "")
        if not self._authorization or not secrets.compare_digest(
            presented.encode(), self._authorization.encode()
        ):
            await _refuse(send, 401, "authentication_error", "Unknown credentials.")
            return
        if (scope["method"], scope["path"]) not in self._routes:
            await _refuse(send, 403, "permission_error", "Not a forwarded route.")
            return
        try:
            body = await _read_body(receive)
        except _Disconnected:
            return
        if body is None:
            await _refuse(send, 413, "request_too_large", "Request is too large.")
            return
        await self._forward(scope, headers, body, send)

    async def _forward(
        self,
        scope: dict[str, Any],
        headers: list[tuple[str, str]],
        body: bytes,
        send: Any,
    ) -> None:
        assert self._client is not None
        query = scope.get("query_string", b"").decode("latin-1")
        url = self._broker.upstream + scope["path"] + (f"?{query}" if query else "")
        forwarded = [(k, v) for k, v in headers if k not in _DROPPED_REQUEST_HEADERS]
        refused: str | None = None
        try:
            while True:
                token = await self._tokens(refused)
                request = self._client.build_request(
                    scope["method"],
                    url,
                    headers=[*forwarded, ("authorization", f"Bearer {token}")],
                    content=body,
                )
                response = await self._client.send(request, stream=True)
                if (
                    response.status_code != httpx.codes.UNAUTHORIZED
                    or refused is not None
                ):
                    break
                await response.aclose()
                refused = token
        except CredentialUnavailableError as exc:
            await _refuse(send, 401, "authentication_error", str(exc))
            return
        except httpx.HTTPError as exc:
            await _refuse(send, 502, "api_error", type(exc).__name__)
            return
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": [
                        (name, value)
                        for name, value in response.headers.raw
                        if name.decode("latin-1").lower() not in _HOP_BY_HOP
                    ],
                }
            )
            async for chunk in response.aiter_raw():
                await send(
                    {"type": "http.response.body", "body": chunk, "more_body": True}
                )
            await send({"type": "http.response.body", "body": b""})
        finally:
            await response.aclose()


class _Disconnected(Exception):
    """The guest went away before its request was whole."""


async def _read_body(receive: Any) -> bytes | None:
    """The whole request body, or None once it passes the limit.

    Raises:
        _Disconnected: When the guest leaves first; a partial body is never
            sent on with the real token.
    """
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _Disconnected
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > _MAX_REQUEST_BYTES:
            return None
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)


async def _refuse(send: Any, status: int, kind: str, message: str) -> None:
    """Answer the guest without reaching the upstream."""
    body = json.dumps(
        {"type": "error", "error": {"type": kind, "message": message}}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
