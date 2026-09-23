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

A tool that takes its API over HTTPS only is answered over TLS, as the guest
alias and as each host the turn relays here, with a certificate from a CA
made for the turn (:attr:`CredentialGateway.ca_pem`). Its key never leaves
this process's memory but for the moment it takes to load it.
"""

from __future__ import annotations

import json
import secrets
import ssl
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from logging import getLogger
from pathlib import Path
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from guildbotics.intelligences.agent_environment.spec import GUEST_HOST_ALIAS
from guildbotics.intelligences.cli_agents import CredentialBroker
from guildbotics.utils.loopback_server import LoopbackServer

#: Hands out the access token to send; given the token the upstream just
#: refused, it refreshes the login unless that was done already.
TokenSource = Callable[[str | None], Awaitable[str]]

_MAX_REQUEST_BYTES = 64 * 1024 * 1024
#: A turn's CA outlives any turn; it is trusted by that turn's microVM alone.
_TLS_LIFETIME = timedelta(days=2)
_LOGGER = getLogger(__name__)
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
#: for the gateway, the credentials it holds, a length the forwarded body
#: sets again, and the encodings it takes -- an answer comes back plain, so
#: that the token is found in it wherever it is.
_DROPPED_REQUEST_HEADERS = _HOP_BY_HOP | {
    "host",
    "authorization",
    "x-api-key",
    "cookie",
    "content-length",
    "accept-encoding",
}


class CredentialUnavailableError(RuntimeError):
    """The login cannot give a token; the message says what to do."""


class CredentialGateway:
    """One turn's gateway to its tool's API."""

    def __init__(
        self,
        broker: CredentialBroker,
        tokens: TokenSource,
        stand_in: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._broker = broker
        self._tokens = tokens
        self._authorization = ""
        #: The turn's secret: the one credential the gateway takes.
        self.stand_in = stand_in
        #: The CA a turn trusts the gateway's TLS by, once it is started.
        self.ca_pem = b""
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
        scheme = "https" if self._broker.tls else "http"
        url = f"{scheme}://{GUEST_HOST_ALIAS}:{self.port}{self._broker.base_url_path}"
        return {
            **dict.fromkeys(self._broker.base_url_env, url),
            **dict(self._broker.turn_environment),
        }

    async def start(self) -> None:
        """Start accepting the turn's stand-in."""
        tls = None
        if self._broker.tls:
            tls, self.ca_pem = _turn_tls(
                (GUEST_HOST_ALIAS, *self._broker.relayed_hosts)
            )
        self._authorization = f"Bearer {self.stand_in}"
        self._client = httpx.AsyncClient(
            transport=self._transport, follow_redirects=False, timeout=_TIMEOUT
        )
        try:
            self._server = await LoopbackServer.start(lambda _port: self, tls=tls)
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
        origin = self._broker.origin(scope["method"], scope["path"])
        if origin is None:
            # What a tool asks for that its catalog does not name, and no more.
            _LOGGER.info("Gateway refused %s %s", scope["method"], scope["path"])
            await _refuse(send, 403, "permission_error", "Not a forwarded route.")
            return
        try:
            body = await _read_body(receive)
        except _Disconnected:
            return
        if body is None:
            await _refuse(send, 413, "request_too_large", "Request is too large.")
            return
        await self._forward(scope, origin, headers, body, send)

    async def _forward(
        self,
        scope: dict[str, Any],
        origin: str,
        headers: list[tuple[str, str]],
        body: bytes,
        send: Any,
    ) -> None:
        assert self._client is not None
        query = scope.get("query_string", b"").decode("latin-1")
        url = origin + scope["path"] + (f"?{query}" if query else "")
        forwarded = [(k, v) for k, v in headers if k not in _DROPPED_REQUEST_HEADERS]
        refused: str | None = None
        try:
            while True:
                token = await self._tokens(refused)
                request = self._client.build_request(
                    scope["method"],
                    url,
                    headers=[
                        *forwarded,
                        ("accept-encoding", "identity"),
                        ("authorization", f"Bearer {token}"),
                    ],
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
        encoding = response.headers.get("content-encoding", "").strip()
        if encoding.lower() not in ("", "identity"):
            # Only a plain answer is searched for the token: one encoded all
            # the same would carry it past the mask to where the tool decodes it.
            # What the upstream said is not logged either, the encoding included:
            # the log names only what the guest asked for.
            await response.aclose()
            _LOGGER.warning(
                "Gateway refused an encoded answer to %s %s",
                scope["method"],
                scope["path"],
            )
            await _refuse(send, 502, "api_error", "The answer is encoded.")
            return
        # An answer never carries the token back into the turn, even one that
        # echoes it: it is masked, to the same length, wherever it stands.
        secret = token.encode()
        mask = b"*" * len(secret)
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": [
                        (name, value.replace(secret, mask))
                        for name, value in response.headers.raw
                        if name.decode("latin-1").lower() not in _HOP_BY_HOP
                    ],
                }
            )
            held = b""
            async for chunk in response.aiter_raw():
                data = (held + chunk).replace(secret, mask)
                # What may be the start of the token is held for the next chunk.
                cut = len(data) - _partial(data, secret)
                data, held = data[:cut], data[cut:]
                await send(
                    {"type": "http.response.body", "body": data, "more_body": True}
                )
            await send({"type": "http.response.body", "body": held})
        finally:
            await response.aclose()


def _partial(data: bytes, secret: bytes) -> int:
    """How long an end of ``data`` is that ``secret`` begins with."""
    for length in range(min(len(data), len(secret) - 1), 0, -1):
        if data.endswith(secret[:length]):
            return length
    return 0


def _turn_tls(names: tuple[str, ...]) -> tuple[ssl.SSLContext, bytes]:
    """A server context for ``names``, from a CA of its own, and that CA."""
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "GuildBotics turn gateway")]
    )
    ca = (
        _certificate(ca_name, ca_name, ca_key.public_key(), now)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    certificate = (
        _certificate(subject, ca_name, key.public_key(), now)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in names]),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.set_alpn_protocols(["http/1.1"])  # HTTP/2 is refused, not guessed.
    with tempfile.TemporaryDirectory() as held:  # The context loads files only.
        chain, private = Path(held, "chain.pem"), Path(held, "key.pem")
        chain.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        private.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        context.load_cert_chain(chain, private)
    return context, ca.public_bytes(serialization.Encoding.PEM)


def _certificate(
    subject: x509.Name, issuer: x509.Name, key: Any, now: datetime
) -> x509.CertificateBuilder:
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + _TLS_LIFETIME)
    )


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
