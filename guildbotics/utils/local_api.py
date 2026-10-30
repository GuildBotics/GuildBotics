"""How a client on this machine reaches the GuildBotics server's API, and the
credential it carries there.

The server listens on a fixed loopback port. On the machine it runs on, the
owner token file beside its records is the credential: whoever can read the
file is the owner. A machine that reaches the server through SSH forwarding
cannot read that file, and holds a token of its own in its OS keychain instead,
given through the consent screen.
"""

import hashlib
import hmac
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from guildbotics.utils.fileio import get_machine_state_path

DEFAULT_PORT = 8765
#: Moves the port for every program on this machine at once: the server, its
#: clients, and the Desktop that launches it.
PORT_ENV = "GUILDBOTICS_SERVER_PORT"
API_PREFIX = "/api"
PROOF_PATH = "/local-api/proof"
#: The nonce is fixed-length lowercase hex, so it can never contain the
#: separator of the proof message.
NONCE_PATTERN = "^[0-9a-f]{64}$"
TOKEN_ID_PATTERN = "^[0-9a-f]{16}$"
_TOKEN_PREFIX = "gbt"
_KEYCHAIN_SERVICE = "guildbotics-server"
_PROOF_TIMEOUT_SECONDS = 2.0


def server_port() -> int:
    """The port the server listens on, as this machine names it."""
    raw = os.environ.get(PORT_ENV, "").strip() or str(DEFAULT_PORT)
    if not raw.isdigit() or int(raw) not in range(1, 65536):
        raise ValueError(f"{PORT_ENV} must name a TCP port, not {raw!r}.")
    return int(raw)


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def server_directory(port: int) -> Path:
    """Where the server on ``port`` keeps its credentials: one server, and so
    one writer, per port."""
    return get_machine_state_path("server", str(port))


def owner_token_path(port: int) -> Path:
    return server_directory(port) / "owner.token"


def new_token() -> tuple[str, str, str]:
    """A fresh token: its public id, its secret's digest, and its text."""
    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    return token_id, token_digest(secret), f"{_TOKEN_PREFIX}_{token_id}_{secret}"


def parse_token(text: str) -> tuple[str, str] | None:
    """The id and the secret's digest of a token text, or None for no token."""
    prefix, _, rest = text.strip().partition("_")
    token_id, _, secret = rest.partition("_")
    if (
        prefix != _TOKEN_PREFIX
        or not secret
        or not re.fullmatch(TOKEN_ID_PATTERN, token_id)
    ):
        return None
    return token_id, token_digest(secret)


def token_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def local_api_proof(digest: str, nonce: str, token_id: str) -> str:
    """Return the HMAC that only the holder of a token's digest can compute.

    Every field before the last has a fixed length or a fixed value, so the
    newline-joined message has exactly one reading.
    """
    message = f"guildbotics-local-api-proof\n{nonce}\n{token_id}"
    return hmac.new(digest.encode(), message.encode(), hashlib.sha256).hexdigest()


def client_token(port: int) -> str | None:
    """The credential this machine holds for the server on ``port``: the
    owner token file where the server runs, else the consent token."""
    try:
        return owner_token_path(port).read_text("utf-8").strip() or None
    except OSError:
        pass
    from keyring.errors import KeyringError

    from guildbotics.utils.keychain import SecretStoreError, system_keychain

    # A machine without a usable keychain (no backend, locked over SSH) holds
    # no consent token: its callers then run where they are.
    try:
        return system_keychain().get_password(_KEYCHAIN_SERVICE, _account(port))
    except SecretStoreError, KeyringError:
        return None


def keep_client_token(port: int, token: str) -> None:
    """Keep the consent token in this machine's keychain."""
    from guildbotics.utils.keychain import system_keychain

    system_keychain().set_password(_KEYCHAIN_SERVICE, _account(port), token)


def _account(port: int) -> str:
    return f"127.0.0.1:{port}"


@contextmanager
def connect_local_api(
    *, headers: dict[str, str] | None = None, timeout: float | None
) -> Iterator[httpx.Client | None]:
    """Yield a client carrying this machine's credential, or None without
    sending it.

    Whatever listens on the port first sees only a token id and a fresh nonce,
    and the token is attached only after it answers with the proof that it
    knows the token's digest. A port taken over while the server is down, or a
    replayed answer, therefore never receives the token or anything sent with
    it. The proof is the only evidence: a health answer says nothing about who
    holds the port.
    """
    port = server_port()
    token = client_token(port)
    parsed = parse_token(token) if token else None
    if token is None or parsed is None:
        yield None
        return
    with httpx.Client(
        base_url=base_url(port) + API_PREFIX,
        headers=headers,
        trust_env=False,
        timeout=timeout,
    ) as client:
        if not _proves_token(client, *parsed):
            yield None
            return
        # Later requests usually reuse the connection the proof came over.
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


def _proves_token(client: httpx.Client, token_id: str, digest: str) -> bool:
    nonce = secrets.token_hex(32)
    expected = local_api_proof(digest, nonce, token_id)
    try:
        response = client.post(
            PROOF_PATH,
            json={"token_id": token_id, "nonce": nonce},
            timeout=_PROOF_TIMEOUT_SECONDS,
        )
        proof = response.raise_for_status().json().get("proof")
    except httpx.HTTPError, ValueError, AttributeError:
        return False
    return isinstance(proof, str) and hmac.compare_digest(
        proof.encode(), expected.encode()
    )
