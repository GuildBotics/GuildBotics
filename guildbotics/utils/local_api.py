"""Device-local discovery record for a running Desktop API, and the only way to
reach that API with its session token."""

import hashlib
import hmac
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from guildbotics.utils.fileio import atomic_write_text, get_machine_state_path

TOKEN_HEADER = "X-GuildBotics-Session-Token"
PROOF_PATH = "/local-api/proof"
#: The nonce is fixed-length lowercase hex, so it can never contain the
#: separator of the proof message.
NONCE_PATTERN = "^[0-9a-f]{64}$"
_PROOF_TIMEOUT_SECONDS = 2.0


class LocalApiEndpoint(BaseModel):
    """A private endpoint; the token must never appear in logs or argv."""

    model_config = ConfigDict(strict=True)

    port: int = Field(gt=0, le=65535)
    token: str = Field(min_length=1, repr=False)
    service_instance_id: str = Field(min_length=1)
    workspace: Path | None

    def publish(self) -> None:
        # atomic_write_text uses a private (0600) temporary file and rename.
        atomic_write_text(endpoint_path(), self.model_dump_json())

    def discard(self) -> None:
        current = read_endpoint()
        if current and current.service_instance_id == self.service_instance_id:
            endpoint_path().unlink(missing_ok=True)


def endpoint_path() -> Path:
    return get_machine_state_path("run", "app-api.json")


def read_endpoint() -> LocalApiEndpoint | None:
    try:
        return LocalApiEndpoint.model_validate_json(endpoint_path().read_bytes())
    except (OSError, ValidationError):
        return None


def local_api_proof(token: str, nonce: str, service_instance_id: str) -> str:
    """Return the HMAC that only the holder of ``token`` can compute.

    Every field before the last has a fixed length or a fixed value, so the
    newline-joined message has exactly one reading.
    """
    message = f"guildbotics-local-api-proof\n{nonce}\n{service_instance_id}"
    return hmac.new(token.encode(), message.encode(), hashlib.sha256).hexdigest()


@contextmanager
def connect_local_api(
    *, headers: dict[str, str] | None = None, timeout: float | None
) -> Iterator[httpx.Client | None]:
    """Yield a client carrying the session token, or None without sending it.

    Whatever listens on the recorded port first sees only a fresh nonce, and
    the token is attached only after it answers with the proof for that nonce
    and the recorded instance. A port taken over after the API exited, or a
    replayed answer, therefore never receives the token or anything sent with
    it. The proof is the only evidence: a live PID or a health answer says
    nothing about who holds the port.
    """
    endpoint = read_endpoint()
    if endpoint is None:
        yield None
        return
    with httpx.Client(
        base_url=f"http://127.0.0.1:{endpoint.port}",
        headers=headers,
        trust_env=False,
        timeout=timeout,
    ) as client:
        if not _proves_token(client, endpoint):
            yield None
            return
        # Later requests usually reuse the connection the proof came over.
        client.headers[TOKEN_HEADER] = endpoint.token
        yield client


def _proves_token(client: httpx.Client, endpoint: LocalApiEndpoint) -> bool:
    nonce = secrets.token_hex(32)
    expected = local_api_proof(endpoint.token, nonce, endpoint.service_instance_id)
    try:
        response = client.post(
            PROOF_PATH, json={"nonce": nonce}, timeout=_PROOF_TIMEOUT_SECONDS
        )
        proof = response.raise_for_status().json().get("proof")
    except (httpx.HTTPError, ValueError, AttributeError):
        return False
    return isinstance(proof, str) and hmac.compare_digest(
        proof.encode(), expected.encode()
    )
