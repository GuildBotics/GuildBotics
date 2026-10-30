"""Who may use the server, and how a browser comes to be one of them.

There is one person, the owner, and no sign-in screen (as in Branvil). The
owner token file beside the server's records is the owner: whoever can read it
can ask for an entry link, a single-use code in a URL fragment that a browser
exchanges for its session cookie. A machine that cannot read the file gets an
owner token of its own through the consent screen and PKCE.

Every request, whatever its route, passes ``guard`` first: the ``Host`` must be
this server's loopback address and an ``Origin`` must be that same origin,
against DNS rebinding and cross-site requests. No CORS header is ever sent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from guildbotics.app_api.errors import AppApiError, api_error_message
from guildbotics.utils.fileio import atomic_write_text
from guildbotics.utils.local_api import (
    NONCE_PATTERN,
    TOKEN_ID_PATTERN,
    base_url,
    local_api_proof,
    new_token,
    owner_token_path,
    parse_token,
    server_directory,
)

#: Followed by the port: a browser sends a cookie to every port of a host, so
#: one name would let the servers of two ports, or Branvil's
#: ``branvil_session``, end each other's sessions.
COOKIE_PREFIX = "guildbotics_session"
CSRF_HEADER = "X-GuildBotics-CSRF"
CODE_TTL_SECONDS = 60
SESSION_SECONDS = 30 * 24 * 3600
_OWNER_FILE = "owner-file"
#: The header the screen sends naming its display language, so error
#: sentences come back in the language the screen is in.
LANGUAGE_HEADER = "X-GuildBotics-Language"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_MAX_STATE_BYTES = 512
_CLIENT = re.compile(r"[A-Za-z0-9 ._-]{1,64}")
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
#: A path of this server: not ``//host`` or ``/\host``, which a browser reads
#: as another host.
_SAME_ORIGIN_PATH = re.compile(r"/(?![/\\])[^\s#]*")
SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self' ipc: http://ipc.localhost; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


class _Token(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    digest: str
    created_at: str


class _Session(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csrf: str
    expires_at: float


class _Records(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tokens: dict[str, _Token] = Field(default_factory=dict)
    #: By the SHA-256 of the cookie value.
    sessions: dict[str, _Session] = Field(default_factory=dict)


class _Consent(BaseModel):
    client: str
    redirect_uri: str
    challenge: str
    expires_at: float


class EntryLinkRequest(BaseModel):
    #: Where the browser lands once in: a path of this server.
    next: str = Field(default="/", max_length=2048)

    @field_validator("next")
    @classmethod
    def _same_origin(cls, value: str) -> str:
        if not _SAME_ORIGIN_PATH.fullmatch(value):
            raise ValueError("next must be a path of this server")
        return value


class EntryLink(BaseModel):
    url: str
    expires_at: str


class SessionRequest(BaseModel):
    code: str = Field(min_length=1, max_length=128)


class SessionResponse(BaseModel):
    csrf: str


class AuthorizationRequest(BaseModel):
    client: str
    redirect_uri: str
    state: str
    code_challenge: str


class AuthorizationResponse(BaseModel):
    redirect: str


class AppTokenRequest(BaseModel):
    code: str = Field(min_length=1, max_length=128)
    redirect_uri: str
    code_verifier: str


class AppToken(BaseModel):
    id: str
    label: str
    token: str
    created_at: str


class ProofRequest(BaseModel):
    token_id: str = Field(pattern=TOKEN_ID_PATTERN)
    nonce: str = Field(pattern=NONCE_PATTERN)


class ProofResponse(BaseModel):
    proof: str


class ServerAuth:
    """The server's credentials: tokens and sessions on disk, kept only as
    digests, and the single-use codes in memory, so a restart spends them."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.base_url = base_url(port)
        self.cookie = f"{COOKIE_PREFIX}_{port}"
        self._directory = server_directory(port)
        self._lock = threading.Lock()
        #: Each code's expiry, by its digest.
        self._entries: dict[str, float] = {}
        self._consents: dict[str, _Consent] = {}
        self._records = self._load()
        self._keep_owner_token()

    # -- credentials ----------------------------------------------------------

    def authenticate(self, request: Request) -> None:
        """Let the owner through: a Bearer token, or the session cookie with
        that session's CSRF value on every write."""
        authorization = request.headers.get("authorization")
        if authorization is not None:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not self._known_token(token):
                raise AppApiError("unauthorized", status_code=401)
            return
        csrf = self._session_csrf(request.cookies.get(self.cookie), renew=True)
        if csrf is None:
            raise AppApiError("unauthorized", status_code=401)
        if request.method not in _SAFE_METHODS and not hmac.compare_digest(
            request.headers.get(CSRF_HEADER, "").encode(), csrf.encode()
        ):
            raise AppApiError("csrf_mismatch", status_code=403)

    def admits(self, websocket: WebSocket) -> bool:
        """Whether a WebSocket handshake comes from a page of this server
        with a session: the browser API sets no header of its own."""
        return self._session_csrf(
            websocket.cookies.get(self.cookie)
        ) is not None and websocket.headers.get("origin") == _origin(
            websocket.headers.get("host", "")
        )

    def proof(self, request: ProofRequest) -> ProofResponse:
        token = self._records.tokens.get(request.token_id)
        if token is None:
            raise AppApiError("unauthorized", status_code=401)
        return ProofResponse(
            proof=local_api_proof(token.digest, request.nonce, request.token_id)
        )

    # -- entering the GUI -----------------------------------------------------

    def entry_link(self, request: EntryLinkRequest) -> EntryLink:
        code = secrets.token_urlsafe(32)
        expires_at = time.time() + CODE_TTL_SECONDS
        with self._lock:
            self._entries = {
                key: expiry for key, expiry in self._entries.items() if expiry > _now()
            }
            self._entries[_digest(code)] = expires_at
        query = "" if request.next == "/" else "?" + urlencode({"next": request.next})
        return EntryLink(
            url=f"{self.base_url}/-/enter{query}#{code}",
            expires_at=_iso(expires_at),
        )

    def start_session(self, request: SessionRequest) -> tuple[str, SessionResponse]:
        """Spend an entry code: the new session's cookie value and CSRF."""
        with self._lock:
            expiry = self._entries.pop(_digest(request.code), None)
            if expiry is None or expiry <= _now():
                raise AppApiError("unauthorized", status_code=401)
            cookie = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            records = self._records.model_copy(deep=True)
            records.sessions = {
                key: session
                for key, session in records.sessions.items()
                if session.expires_at > _now()
            }
            records.sessions[_digest(cookie)] = _Session(
                csrf=csrf, expires_at=_now() + SESSION_SECONDS
            )
            self._save(records)
        return cookie, SessionResponse(csrf=csrf)

    def session(self, request: Request) -> SessionResponse:
        csrf = self._session_csrf(request.cookies.get(self.cookie))
        if csrf is None:
            raise AppApiError("unauthorized", status_code=401)
        return SessionResponse(csrf=csrf)

    # -- local apps -----------------------------------------------------------

    def authorize(self, request: AuthorizationRequest) -> AuthorizationResponse:
        """Allow a local app: a single-use code for its loopback address, bound
        to its name, that address, and its PKCE challenge."""
        if not (
            _CLIENT.fullmatch(request.client)
            and request.client.strip()
            and is_loopback_redirect(request.redirect_uri)
            and 1 <= len(request.state.encode()) <= _MAX_STATE_BYTES
            and _CHALLENGE.fullmatch(request.code_challenge)
        ):
            raise AppApiError("invalid", status_code=400)
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._consents = {
                key: consent
                for key, consent in self._consents.items()
                if consent.expires_at > _now()
            }
            self._consents[_digest(code)] = _Consent(
                client=request.client,
                redirect_uri=request.redirect_uri,
                challenge=request.code_challenge,
                expires_at=_now() + CODE_TTL_SECONDS,
            )
        return AuthorizationResponse(
            redirect=_with_query(
                request.redirect_uri, {"code": code, "state": request.state}
            )
        )

    def app_token(self, request: AppTokenRequest) -> AppToken:
        """Spend a consent code for the app's owner token, which replaces the
        one it was given before."""
        with self._lock:
            consent = self._consents.pop(_digest(request.code), None)
            if (
                consent is None
                or consent.expires_at <= _now()
                or consent.redirect_uri != request.redirect_uri
                or not _VERIFIER.fullmatch(request.code_verifier)
                or not hmac.compare_digest(
                    _challenge(request.code_verifier).encode(),
                    consent.challenge.encode(),
                )
            ):
                raise AppApiError("invalid", status_code=400)
            label = f"app:{consent.client}"
            token_id, digest, text = new_token()
            created_at = _iso(_now())
            records = self._records.model_copy(deep=True)
            records.tokens = {
                key: token
                for key, token in records.tokens.items()
                if token.label != label
            }
            records.tokens[token_id] = _Token(
                label=label, digest=digest, created_at=created_at
            )
            self._save(records)
        return AppToken(id=token_id, label=label, token=text, created_at=created_at)

    # -- records --------------------------------------------------------------

    def _known_token(self, text: str) -> bool:
        parsed = parse_token(text)
        token = self._records.tokens.get(parsed[0]) if parsed else None
        return (
            parsed is not None
            and token is not None
            and hmac.compare_digest(token.digest.encode(), parsed[1].encode())
        )

    def _session_csrf(self, cookie: str | None, *, renew: bool = False) -> str | None:
        """The CSRF value of the cookie's live session. A session in use lives
        on: once half its time has passed, a request renews it, so a screen
        left open (a Desktop window for months) never finds itself signed out.
        """
        key = _digest(cookie) if cookie else ""
        session = self._records.sessions.get(key)
        if session is None or session.expires_at <= _now():
            return None
        if renew and session.expires_at - _now() < SESSION_SECONDS / 2:
            with self._lock:
                records = self._records.model_copy(deep=True)
                records.sessions[key] = _Session(
                    csrf=session.csrf, expires_at=_now() + SESSION_SECONDS
                )
                self._save(records)
        return session.csrf

    def _keep_owner_token(self) -> None:
        """Keep a token in the owner file that authenticates. When the file is
        missing or does not (replaced, restored without it), its previous token
        is revoked and every session ends with it."""
        path = owner_token_path(self.port)
        try:
            if self._known_token(path.read_text("utf-8")):
                return
        except OSError:
            pass
        token_id, digest, text = new_token()
        records = _Records(
            tokens={
                key: token
                for key, token in self._records.tokens.items()
                if token.label != _OWNER_FILE
            }
        )
        records.tokens[token_id] = _Token(
            label=_OWNER_FILE, digest=digest, created_at=_iso(_now())
        )
        # The records first: a token file the records do not know would end
        # every session again at the next start.
        self._save(records)
        atomic_write_text(path, text + "\n")

    def _load(self) -> _Records:
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            return _Records.model_validate_json(self._path().read_bytes())
        except OSError, ValidationError:
            return _Records()

    def _save(self, records: _Records) -> None:
        # atomic_write_text writes a private (0600) temporary file and renames.
        atomic_write_text(self._path(), records.model_dump_json())
        self._records = records

    def _path(self) -> Path:
        return self._directory / "auth.json"


def is_loopback_redirect(uri: str) -> bool:
    """Whether a consent may send the browser there: plain http on this
    machine's loopback, so the code never leaves the browser's machine."""
    try:
        parts = urlsplit(uri)
        hostname = parts.hostname
        parts.port  # noqa: B018 -- raises on a malformed port
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and hostname in _LOOPBACK
        and "@" not in parts.netloc
        and not parts.fragment
        and "#" not in uri
    )


class Guard:
    """Refuse a request for another host or from another origin before
    anything runs for it, and give every response the security headers."""

    def __init__(self, app: ASGIApp, port: int) -> None:
        self.app = app
        self._hosts = {f"{host}:{port}" for host in ("127.0.0.1", "localhost", "[::1]")}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in scope["headers"]
        }
        host = headers.get("host", "")
        origin = headers.get("origin")
        if host not in self._hosts:
            refusal = (421, "misdirected")
        elif origin is not None and origin != _origin(host):
            refusal = (403, "origin_mismatch")
        else:
            await self.app(scope, receive, _secured(send))
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        status, code = refusal
        language = (
            "ja" if headers.get(LANGUAGE_HEADER.lower(), "").startswith("ja") else "en"
        )
        response = JSONResponse(
            status_code=status,
            content={
                "code": code,
                "message": api_error_message(code, language, {}),
                "context": {},
            },
        )
        await response(scope, receive, _secured(send))


def _secured(send: Send) -> Send:
    """``send`` adding the security headers a response does not set itself:
    a page may narrow the policy for what it does."""

    async def secured(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            present = {key.lower() for key, _ in headers}
            message["headers"] = headers + [
                (key.encode(), value.encode())
                for key, value in SECURITY_HEADERS.items()
                if key.encode() not in present
            ]
        await send(message)

    return secured


def _now() -> float:
    return time.time()


def _origin(host: str) -> str:
    return f"http://{host}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _with_query(uri: str, added: dict[str, str]) -> str:
    parts = urlsplit(uri)
    query = urlencode([*parse_qsl(parts.query, keep_blank_values=True), *added.items()])
    return urlunsplit(parts._replace(query=query))


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")
