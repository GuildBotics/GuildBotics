"""Workspace-scoped secret storage.

Secrets (LLM API keys, GitHub/Slack tokens) live in the operating system
keychain whenever a functional backend is available (macOS Keychain, Windows
Credential Manager, Linux Secret Service). The workspace keeps only a
non-secret index file (``.guildbotics/config/secrets.yml``) naming the stored
keys, so the workspace stays portable while the secret values stay out of
plaintext files. Workspaces without that index file (e.g. created on a
keyring-less machine) use ``.env`` storage instead.

Resolution precedence for a secret value is: real environment variable >
OS keychain > ``.env`` file. Servers and CI therefore keep working with plain
environment variables regardless of the configured backend.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from guildbotics.utils.fileio import load_yaml_dict, save_yaml_file

SECRETS_BACKEND_ENV = "GUILDBOTICS_SECRETS_BACKEND"
KEYRING_BACKEND = "keyring"
ENV_FILE_BACKEND = "env-file"
SECRETS_INDEX_FILENAME = "secrets.yml"
_KEYRING_SERVICE_PREFIX = "GuildBotics"
# Values that dotenv can reproduce without quoting; anything else (newlines,
# quotes, spaces, comments, backslashes) is written double-quoted with escapes
# so that multi-line secrets such as PEM keys survive a write/read round-trip.
_PLAIN_ENV_VALUE = re.compile(r"[^\s#'\"\\]*")

# Secrets that must never be published to process environment variables:
# child processes (AI CLI tools in particular) inherit the full environment,
# so high-value material like a GitHub App private key would leak into every
# agent subprocess. Consumers read these from the store at the point of use.
ENVIRONMENT_EXCLUDED_SECRET_SUFFIXES = ("_GITHUB_PRIVATE_KEY",)


def is_environment_secret(key: str) -> bool:
    """True when the secret may be published to ``os.environ``."""
    return not key.endswith(ENVIRONMENT_EXCLUDED_SECRET_SUFFIXES)


def read_env_values(env_file: Path) -> dict[str, str]:
    """Read a dotenv file into a plain ``{key: value}`` mapping."""
    raw_values = dict(dotenv_values(env_file)) if env_file.exists() else {}
    return {
        str(key): str(value) for key, value in raw_values.items() if value is not None
    }


def write_env_text(env_file: Path, text: str) -> None:
    """Write dotenv content atomically, owner-only from the moment it exists.

    ``mkstemp`` creates the temp file with mode 0600, so the content is never
    readable by other users — not even between creation and a later chmod —
    and ``os.replace`` swaps it in atomically.
    """
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

    backend: str
    location: Path
    """The workspace file backing this store (the .env file, or the
    non-secret keychain index)."""

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

    def values(self) -> dict[str, str]:
        """Return every stored key with its value."""
        result: dict[str, str] = {}
        for key in self.keys():
            value = self.get(key)
            if value is not None:
                result[key] = value
        return result


class EnvFileSecretStore(SecretStore):
    """Plaintext storage: secrets sit in the workspace ``.env`` file."""

    backend = ENV_FILE_BACKEND

    def __init__(self, env_file: Path):
        self.location = env_file

    def get(self, key: str) -> str | None:
        return read_env_values(self.location).get(key)

    def set(self, key: str, value: str) -> None:
        values = read_env_values(self.location)
        values[key] = value
        write_env_values(self.location, values)

    def delete(self, key: str) -> None:
        values = read_env_values(self.location)
        if values.pop(key, None) is not None:
            write_env_values(self.location, values)

    def keys(self) -> list[str]:
        return list(read_env_values(self.location))


class KeyringSecretStore(SecretStore):
    """OS keychain storage with a non-secret key index in the workspace.

    The index file records the backend choice, a stable ``store_id`` (the
    keychain namespace, so moving the workspace directory keeps its secrets),
    and the stored key names — keyring backends cannot enumerate entries.
    """

    backend = KEYRING_BACKEND

    def __init__(self, config_dir: Path):
        self.location = config_dir / SECRETS_INDEX_FILENAME

    def get(self, key: str) -> str | None:
        index = self._read_index()
        if key not in index["keys"]:
            return None
        import keyring

        return keyring.get_password(self._service(index), key)

    def set(self, key: str, value: str) -> None:
        import keyring

        index = self._read_index()
        keyring.set_password(self._service(index), key, value)
        if key not in index["keys"]:
            index["keys"].append(key)
        self._write_index(index)

    def delete(self, key: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        index = self._read_index()
        with contextlib.suppress(PasswordDeleteError):
            keyring.delete_password(self._service(index), key)
        if key in index["keys"]:
            index["keys"].remove(key)
            self._write_index(index)

    def keys(self) -> list[str]:
        return list(self._read_index()["keys"])

    def ensure_initialized(self) -> None:
        """Persist the index file, pinning this workspace to this backend."""
        self._write_index(self._read_index())

    def _service(self, index: dict[str, Any]) -> str:
        return f"{_KEYRING_SERVICE_PREFIX}/{index['store_id']}"

    def _read_index(self) -> dict[str, Any]:
        data = load_yaml_dict(self.location)
        store_id = str(data.get("store_id", "")) or uuid.uuid4().hex
        keys = data.get("keys")
        return {
            "backend": KEYRING_BACKEND,
            "store_id": store_id,
            "keys": [str(key) for key in keys] if isinstance(keys, list) else [],
        }

    def _write_index(self, index: dict[str, Any]) -> None:
        index["keys"] = sorted(set(index["keys"]))
        self.location.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(self.location, index)


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


def configured_secrets_backend(config_dir: Path) -> str | None:
    """Return the backend pinned for this workspace, or ``None`` when unset.

    ``GUILDBOTICS_SECRETS_BACKEND`` overrides the workspace index file so that
    tests, CI, and server deployments can force a backend.
    """
    override = os.getenv(SECRETS_BACKEND_ENV, "").strip()
    if override in {KEYRING_BACKEND, ENV_FILE_BACKEND}:
        return override
    index_file = config_dir / SECRETS_INDEX_FILENAME
    if index_file.exists():
        backend = str(load_yaml_dict(index_file).get("backend", ""))
        if backend in {KEYRING_BACKEND, ENV_FILE_BACKEND}:
            return backend
    return None


def resolve_secret_store(
    config_dir: Path, env_file: Path, *, create_default: bool = False
) -> SecretStore:
    """Return the workspace's secret store.

    Without ``create_default`` an unconfigured workspace uses the ``.env``
    backend. With it (initial project setup), the OS keychain is chosen when
    available.
    """
    backend = configured_secrets_backend(config_dir)
    if backend is None and create_default and keyring_available():
        backend = KEYRING_BACKEND
    if backend == KEYRING_BACKEND:
        return KeyringSecretStore(config_dir)
    return EnvFileSecretStore(env_file)
