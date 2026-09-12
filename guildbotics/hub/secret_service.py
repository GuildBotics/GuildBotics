"""The Hub Secret wire protocol, shared by the CLI and Desktop transport."""

from __future__ import annotations

import io
import json
from typing import Any, Literal

from guildbotics.hub import secret_host, secret_stream
from guildbotics.utils.keychain import InvalidSecretKeyError, SecretStoreError
from guildbotics.utils.secret_store import is_locked_error, keyring_status

Operation = Literal["list", "send", "receive"]
DESKTOP_REQUIRED = "desktop_required"


class HubDesktopRequiredError(secret_host.HubSecretError):
    """The macOS Hub requires its Desktop to perform a secret transfer."""


def handle(
    operation: Operation,
    workspace_id: str,
    payload: bytes = b"",
    keys: tuple[str, ...] = (),
    *,
    unavailable: bool = False,
) -> bytes:
    """Answer one request without exposing values in errors or metadata.

    The unavailable response reads only the generation index, never a value.
    Desktop invokes this handler directly; it must not delegate back to itself.
    """
    held = secret_host.generations(workspace_id)
    if operation == "list":
        status = (
            {"available": False, "locked": False, "error_code": DESKTOP_REQUIRED}
            if unavailable
            else {k: v for k, v in keyring_status().items() if k != "backend"}
        )
        return _json(
            {"workspace_id": workspace_id, "keys": held, "secret_store": status}
        )
    if operation == "receive":
        entries = list(secret_stream.read_entries(payload))
        return _json(
            {
                "results": [
                    {"key": entry.key, "status": DESKTOP_REQUIRED}
                    if unavailable
                    else _store(workspace_id, entry)
                    for entry in entries
                ]
            }
        )
    stream = io.BytesIO()
    for key in keys:
        if unavailable:
            secret_stream.write_entry(stream, key, {"error": DESKTOP_REQUIRED})
            continue
        try:
            value, generation = secret_host.read_secret(workspace_id, key)
        except secret_host.HubSecretMissingError:
            secret_stream.write_entry(stream, key, {"error": "missing"})
        except InvalidSecretKeyError:
            secret_stream.write_entry(stream, key, {"error": "invalid"})
        except SecretStoreError as exc:
            secret_stream.write_entry(stream, key, {"error": _store_error(exc)})
        else:
            secret_stream.write_entry(
                stream, key, {"generation": generation}, value.encode("utf-8")
            )
    return stream.getvalue()


def _store(workspace_id: str, entry: secret_stream.SecretEntry) -> dict[str, Any]:
    try:
        generation = secret_host.store_secret(
            workspace_id,
            entry.key,
            base_generation=_generation(entry, "base"),
            candidate_generation=_generation(entry, "candidate"),
            value=entry.value.decode("utf-8"),
        )
    except secret_host.HubSecretConflictError:
        return {"key": entry.key, "status": "conflict"}
    except (InvalidSecretKeyError, UnicodeDecodeError):
        return {"key": entry.key, "status": "invalid"}
    except SecretStoreError as exc:
        return {"key": entry.key, "status": _store_error(exc)}
    return {"key": entry.key, "status": "stored", "generation": generation}


def _generation(entry: secret_stream.SecretEntry, name: str) -> int:
    value = entry.header.get(f"{name}_generation")
    if isinstance(value, bool) or not isinstance(value, int):
        raise secret_stream.SecretStreamError("a secret entry declares no generation")
    return value


def _store_error(exc: SecretStoreError) -> str:
    return "locked" if is_locked_error(exc) else "store_unavailable"


def _json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    )
