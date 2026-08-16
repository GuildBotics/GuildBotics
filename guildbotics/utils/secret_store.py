"""Workspace-scoped secret storage backed by the OS keychain.

Secrets (LLM API keys, GitHub/Slack tokens) live in the operating system
keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service).
The workspace keeps only a non-secret index file
(``.guildbotics/config/secrets.yml``) naming the stored keys and their
shared generation. Each device records the generations it actually holds in
``.guildbotics/local/secrets.json``. Secret values themselves are never
written to workspace files.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from guildbotics.utils.fileio import (
    get_workspace_config_dir,
    load_yaml_dict,
    save_yaml_file,
)
from guildbotics.utils.keychain import (
    Keychain,
    SecretStoreError,
    system_keychain,
)
from guildbotics.utils.shared_write_lock import shared_write_operation
from guildbotics.utils.workspace_sync_port import notify_shared_state_changed

SECRETS_INDEX_FILENAME = "secrets.yml"
LOCAL_SECRETS_FILENAME = "secrets.json"
_KEYRING_SERVICE_PREFIX = "GuildBotics"
_PLAIN_ENV_VALUE = re.compile(r"[^\s#'\"\\]*")

# Secrets that must never be published to process environment variables:
# child processes (AI CLI tools in particular) inherit the full environment,
# so high-value material like a GitHub App private key would leak into every
# agent subprocess. Consumers read these from the store at the point of use.
ENVIRONMENT_EXCLUDED_SECRET_SUFFIXES = ("_GITHUB_PRIVATE_KEY",)


def is_environment_secret(key: str) -> bool:
    """True when the secret may be published to ``os.environ``."""
    return not key.endswith(ENVIRONMENT_EXCLUDED_SECRET_SUFFIXES)


def utc_now_iso() -> str:
    """Return a UTC timestamp suitable for secret metadata."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_env_values(env_file: Path) -> dict[str, str]:
    """Read a dotenv file into a plain ``{key: value}`` mapping."""
    raw_values = dict(dotenv_values(env_file)) if env_file.exists() else {}
    return {
        str(key): str(value) for key, value in raw_values.items() if value is not None
    }


def write_env_text(env_file: Path, text: str) -> None:
    """Write dotenv content atomically, owner-only from the moment it exists."""
    env_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=env_file.parent, prefix=f".{env_file.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp_name, env_file)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def format_env_line(key: str, value: str) -> str:
    """Serialize one dotenv entry so ``dotenv_values`` reads back ``value``."""
    if _PLAIN_ENV_VALUE.fullmatch(value):
        return f"{key}={value}"
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'{key}="{escaped}"'


def write_env_values(env_file: Path, values: dict[str, str]) -> None:
    """Serialize a ``{key: value}`` mapping as a dotenv file (owner-only)."""
    write_env_text(
        env_file,
        "\n".join(format_env_line(key, value) for key, value in values.items()),
    )


class SecretStore(ABC):
    """Named secret values scoped to one workspace."""

    location: Path

    @abstractmethod
    def get(self, key: str) -> str | None:
        """Return the stored value for ``key``, or ``None`` when absent."""

    @abstractmethod
    def set(self, key: str, value: str) -> None:
        """Store ``value`` under ``key``."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove ``key`` if present."""

    @abstractmethod
    def keys(self) -> list[str]:
        """Return the names of the stored keys."""

    def set_many(self, values: dict[str, str]) -> None:
        """Store multiple values after backend-specific validation."""
        for key, value in values.items():
            self.set(key, value)

    def values(self) -> dict[str, str]:
        """Return every stored key with its value."""
        result: dict[str, str] = {}
        for key in self.keys():
            value = self.get(key)
            if value is not None:
                result[key] = value
        return result


class KeyringSecretStore(SecretStore):
    """OS keychain storage with a non-secret key index in the workspace."""

    def __init__(
        self,
        config_dir: Path,
        *,
        local_index: Path | None = None,
        keychain: Keychain | None = None,
    ):
        self.location = config_dir / SECRETS_INDEX_FILENAME
        # The device-local generation record always belongs to the SAME
        # workspace as ``config_dir`` (its sibling ``local/`` directory);
        # resolving it from ambient environment variables would let the two
        # halves of one store land in different workspaces.
        self._local_index = (
            local_index or config_dir.parent / "local" / LOCAL_SECRETS_FILENAME
        )
        self._keychain = keychain or system_keychain()

    def get(self, key: str) -> str | None:
        index = self._read_index()
        meta = index["keys"].get(key)
        if meta is None:
            return None
        if self._is_stale(key, meta):
            # Another device updated the shared generation; the value this
            # keychain holds is outdated. Never serve it — the key needs a
            # `secrets set` / `secrets import` here (`secrets status` lists it).
            return None
        try:
            return self._keychain.get_password(self._service(index), key)
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(str(exc)) from exc

    def stale_keys(self) -> list[str]:
        """Keys whose shared generation this device does not hold."""
        index = self._read_index()
        return sorted(
            key for key, meta in index["keys"].items() if self._is_stale(key, meta)
        )

    def _is_stale(self, key: str, meta: dict[str, Any]) -> bool:
        shared = _as_generation(meta.get("generation"))
        return shared is not None and self.local_generation(key) != shared

    def set(self, key: str, value: str) -> None:
        self.set_many({key: value})

    @shared_write_operation
    def set_many(self, values: dict[str, str]) -> None:
        index = self._read_index()
        service = self._service(index)
        written_keys: list[str] = []
        try:
            for key, value in values.items():
                self._keychain.validate_password(key, value)
            for key, value in values.items():
                self._keychain.set_password(service, key, value)
                written_keys.append(key)
        except Exception as exc:
            if written_keys:
                self._record_keys(index, written_keys)
            if isinstance(exc, SecretStoreError):
                raise
            raise SecretStoreError(str(exc)) from exc
        self._record_keys(index, written_keys)

    @shared_write_operation
    def delete(self, key: str) -> None:
        index = self._read_index()
        try:
            self._keychain.delete_password(self._service(index), key)
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(str(exc)) from exc
        if key in index["keys"]:
            del index["keys"][key]
            self._write_index(index)
            self._drop_local_generation(key)

    @shared_write_operation
    def rename(self, old_key: str, new_key: str) -> None:
        """Move a key's shared metadata, local generation, and stored value.

        The index entry moves regardless of whether this device holds the
        key's current generation, so shared metadata never keeps a stale
        entry under the old name; a stale value stays stale under the new
        name. No-op when ``old_key`` is not indexed (e.g. a retried rename).
        """
        if old_key == new_key:
            return
        index = self._read_index()
        meta = index["keys"].pop(old_key, None)
        if meta is None:
            return
        service = self._service(index)
        try:
            value = self._keychain.get_password(service, old_key)
            if value is not None:
                self._keychain.set_password(service, new_key, value)
            self._keychain.delete_password(service, old_key)
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(str(exc)) from exc
        index["keys"][new_key] = meta
        self._write_index(index)
        local = self._read_local()
        local_meta = local["keys"].pop(old_key, None)
        if local_meta is not None:
            local["keys"][new_key] = local_meta
            self._write_local(local)

    def keys(self) -> list[str]:
        return list(self._read_index()["keys"])

    @shared_write_operation
    def ensure_initialized(self) -> None:
        """Persist the index file, pinning this workspace to the OS keychain.

        The span starts at the read: what is written is the index that was
        read, so a queue checkout in between would be written back over.
        """
        self._write_index(self._read_index())

    def adopt_shared_generations(self) -> None:
        """Record that this device holds every generation in the shared index.

        Used when a workspace arrives on a device whose keychain already has
        the values (e.g. the one-shot workspace relocation on the same machine).
        """
        index = self._read_index()
        self._align_local_generations(index, list(index["keys"]))

    def shared_generation(self, key: str) -> int | None:
        """Return the shared generation recorded for ``key``, if any."""
        meta = self._read_index()["keys"].get(key)
        if not isinstance(meta, dict):
            return None
        return _as_generation(meta.get("generation"))

    def local_generation(self, key: str) -> int | None:
        """Return the generation this device holds for ``key``, if any."""
        return self._read_local().get("keys", {}).get(key, {}).get("generation")

    def _service(self, index: dict[str, Any]) -> str:
        return f"{_KEYRING_SERVICE_PREFIX}/{index['store_id']}"

    def _record_keys(self, index: dict[str, Any], keys: list[str]) -> None:
        now = utc_now_iso()
        for key in keys:
            current = index["keys"].get(key) or {}
            generation = _as_generation(current.get("generation")) or 0
            index["keys"][key] = {
                "generation": generation + 1,
                "updated_at": now,
            }
        self._write_index(index)
        self._align_local_generations(index, keys)

    def _read_index(self) -> dict[str, Any]:
        data = load_yaml_dict(self.location)
        store_id = str(data.get("store_id", "")) or uuid.uuid4().hex
        return {
            "store_id": store_id,
            "keys": _parse_key_index(data.get("keys")),
        }

    def _write_index(self, index: dict[str, Any]) -> None:
        payload = {
            "store_id": index["store_id"],
            "keys": {key: dict(index["keys"][key]) for key in sorted(index["keys"])},
        }
        self.location.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(self.location, payload)
        notify_shared_state_changed("update", [self.location])

    def _read_local(self) -> dict[str, Any]:
        if not self._local_index.exists():
            return {"schema_version": 1, "keys": {}}
        try:
            data = json.loads(self._local_index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": 1, "keys": {}}
        if not isinstance(data, dict):
            return {"schema_version": 1, "keys": {}}
        raw_keys = data.get("keys")
        keys: dict[str, dict[str, Any]] = {}
        if isinstance(raw_keys, dict):
            for key, meta in raw_keys.items():
                if not isinstance(meta, dict):
                    continue
                generation = _as_generation(meta.get("generation"))
                if generation is None:
                    continue
                keys[str(key)] = {
                    "generation": generation,
                    "pending_send": bool(meta.get("pending_send", False)),
                }
        return {"schema_version": 1, "keys": keys}

    def _write_local(self, payload: dict[str, Any]) -> None:
        self._local_index.parent.mkdir(parents=True, exist_ok=True)
        self._local_index.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _align_local_generations(self, index: dict[str, Any], keys: list[str]) -> None:
        local = self._read_local()
        for key in keys:
            meta = index["keys"].get(key) or {}
            generation = _as_generation(meta.get("generation"))
            if generation is None:
                continue
            local["keys"][key] = {"generation": generation, "pending_send": False}
        self._write_local(local)

    def _drop_local_generation(self, key: str) -> None:
        local = self._read_local()
        if key in local["keys"]:
            del local["keys"][key]
            self._write_local(local)


def keyring_available() -> bool:
    """True when a functional (non-fail, recommended-priority) keychain exists."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        backend = keyring.get_keyring()
        if isinstance(backend, FailKeyring):
            return False
        return float(getattr(backend, "priority", 0)) >= 1
    except Exception:
        return False


def keyring_status() -> dict[str, Any]:
    """Probe the OS keychain with a read so status reflects connectivity.

    The probe never exposes or writes secret values: it reads a well-known
    non-existent entry, which succeeds (returning nothing) on a reachable,
    unlocked keychain and raises when the store is locked or unreachable.
    """
    if not keyring_available():
        return {"available": False, "locked": False, "backend": "unavailable"}
    try:
        system_keychain().get_password(
            f"{_KEYRING_SERVICE_PREFIX}/status-probe", "probe"
        )
    except Exception as exc:
        return {
            "available": False,
            "locked": _is_locked_error(exc),
            "backend": "os-keychain",
        }
    return {"available": True, "locked": False, "backend": "os-keychain"}


def _is_locked_error(exc: BaseException) -> bool:
    try:
        from keyring.errors import KeyringLocked
    except ImportError:
        return False
    return isinstance(exc, KeyringLocked) or "lock" in str(exc).lower()


def resolve_secret_store(
    config_dir: Path | None = None,
    *,
    create_default: bool = False,
) -> KeyringSecretStore:
    """Return the workspace's OS-keychain secret store.

    There is no plaintext fallback. A missing or locked keychain raises
    ``SecretStoreError``.
    """
    if not keyring_available():
        raise SecretStoreError(
            "The OS secret store is unavailable. Unlock it or install a "
            "supported backend, then retry."
        )
    store = KeyringSecretStore(
        config_dir if config_dir is not None else get_workspace_config_dir()
    )
    if create_default:
        store.ensure_initialized()
    return store


def _parse_key_index(raw: object) -> dict[str, dict[str, Any]]:
    keys: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return keys
    for key, meta in raw.items():
        if not isinstance(meta, dict):
            continue
        generation = _as_generation(meta.get("generation"))
        if generation is None:
            continue
        keys[str(key)] = {
            "generation": generation,
            "updated_at": str(meta.get("updated_at") or "") or utc_now_iso(),
        }
    return keys


def _as_generation(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1:
        return None
    return value
