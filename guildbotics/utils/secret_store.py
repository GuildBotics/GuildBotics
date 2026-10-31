"""Workspace-scoped secret storage backed by the OS keychain.

Secrets (LLM API keys, GitHub/Slack tokens) live in the operating system
keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service).
The workspace keeps only a non-secret index file
(``.guildbotics/config/secrets.yml``) naming the stored keys, and records the
keychain namespace the values are held under in
``.guildbotics/local/secrets.json``. Secret values themselves are never written
to workspace files.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate

from dotenv import dotenv_values

from guildbotics.utils.fileio import (
    atomic_write_text,
    get_identity_config_dir,
    get_workspace_config_dir,
    load_yaml_dict,
    save_yaml_file,
    workspace_root_from_config_dir,
)
from guildbotics.utils.keychain import (
    InvalidSecretKeyError,
    Keychain,
    SecretStoreError,
    system_keychain,
)
from guildbotics.utils.shared_write_lock import shared_write_lock

SECRETS_INDEX_FILENAME = "secrets.yml"
LOCAL_SECRETS_FILENAME = "secrets.json"
_KEYRING_SERVICE_PREFIX = "GuildBotics"
_PLAIN_ENV_VALUE = re.compile(r"[^\s#'\"\\]*")
#: What a logical key may be called: a key is published as an environment
#: variable of the same name, so it is held to the characters one may use.
#:
#: A leading digit is allowed. Member keys are named after the member, and a
#: ``person_id`` may start with one.
_SECRET_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_]{0,127}$")

# Secrets that must never be published to process environment variables:
# child processes (AI CLI tools in particular) inherit the full environment,
# so high-value material like a GitHub App private key would leak into every
# agent subprocess. Consumers read these from the store at the point of use.
ENVIRONMENT_EXCLUDED_SECRET_SUFFIXES = ("_GITHUB_PRIVATE_KEY",)

# Name fragments that mark an environment variable as carrying a credential
# value. The set of credentials a process environment holds is open-ended --
# GuildBotics publishes person-prefixed tokens, the operator's shell exports
# whatever else the machine needs -- so membership is decided by the name
# rather than by an enumeration that has to be extended for every new secret.
_SECRET_ENV_NAME_PARTS = ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "API_KEY")

# Environment keys whose values are held by the workspace SecretStore. The
# store accepts any key name (`guildbotics secrets set DATABASE_URL ...`), so
# a name pattern cannot classify these; the fact that the key was stored is
# kept as provenance instead. The set only grows for the process lifetime:
# once a key may have reached `os.environ`, a workspace switch or reload must
# not demote it to an ordinary inheritable variable.
_KNOWN_SECRET_ENV_KEYS: set[str] = set()


def register_secret_env_keys(keys: Iterable[str]) -> None:
    """Record environment keys whose values are held by the SecretStore.

    Callers register key names before deciding whether to fetch or publish
    them, so keys that are skipped (already supplied as real environment
    variables) or whose fetch later fails are still classified as secret.
    """
    _KNOWN_SECRET_ENV_KEYS.update(keys)


def known_secret_env_keys() -> frozenset[str]:
    """Return the environment keys registered as SecretStore-held."""
    return frozenset(_KNOWN_SECRET_ENV_KEYS)


def is_environment_secret(key: str) -> bool:
    """True when the secret may be published to ``os.environ``."""
    return not key.endswith(ENVIRONMENT_EXCLUDED_SECRET_SUFFIXES)


def is_secret_env_key(key: str) -> bool:
    """True when an environment variable carries a credential value.

    A key qualifies by name pattern or by provenance: keys seen in the
    workspace SecretStore are secret whatever they are called. Used to redact
    such values from anything a member writes down.
    """
    if key in _KNOWN_SECRET_ENV_KEYS:
        return True
    upper = key.upper()
    return any(part in upper for part in _SECRET_ENV_NAME_PARTS)


def require_secret_key(key: str) -> str:
    """Return ``key`` once it is certainly a logical secret key name.

    Raises:
        InvalidSecretKeyError: When it is not one.
    """
    if not _SECRET_KEY.match(key or ""):
        # The text is deliberately left out. A key name is not secret, but a
        # caller that has its arguments the wrong way round is holding a value
        # where a name should be, and this message is the one place that would
        # then print it.
        raise InvalidSecretKeyError(
            "A secret key name may hold only letters, digits, and underscores."
        )
    return key


def is_secret_key(key: str) -> bool:
    """True when ``key`` is a logical secret key name."""
    return bool(_SECRET_KEY.match(key or ""))


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


def _store_operation[**P, R](
    func: Callable[Concatenate[KeyringSecretStore, P], R],
) -> Callable[Concatenate[KeyringSecretStore, P], R]:
    """Make the whole method one span of the store's :meth:`write_span`."""

    @wraps(func)
    def wrapper(self: KeyringSecretStore, *args: P.args, **kwargs: P.kwargs) -> R:
        with self.write_span():
            return func(self, *args, **kwargs)

    return wrapper


class KeyringSecretStore(SecretStore):
    """OS keychain storage with a non-secret key index in the workspace.

    Every change here reads the index, moves values in the keychain, and
    writes the index back, so the whole operation is one span rather than the
    write at the end of it: read outside one, the index that gets written back
    drops the keys another writer added in between. The operations hold
    :meth:`write_span` for the whole method.
    """

    def __init__(
        self,
        config_dir: Path,
        *,
        local_index: Path | None = None,
        keychain: Keychain | None = None,
    ):
        self.location = config_dir / SECRETS_INDEX_FILENAME
        # The namespace record always belongs to the SAME workspace as
        # ``config_dir`` (its sibling ``local/`` directory); resolving it from
        # ambient environment variables would let the two halves of one store
        # land in different workspaces.
        self._local_index = (
            local_index or config_dir.parent / "local" / LOCAL_SECRETS_FILENAME
        )
        self._keychain = keychain or system_keychain()

    @contextlib.contextmanager
    def write_span(self) -> Iterator[None]:
        """Hold the shared-write lock of the workspace this store belongs to.

        That is the workspace whose ``config/`` holds the index, which is not
        necessarily the selected one: the lock has to be the one the other
        writers of this index take, or it serializes against nothing. A store
        outside any workspace takes no lock.
        """
        root = workspace_root_from_config_dir(self.location.parent)
        if root is None:
            yield
            return
        with shared_write_lock(root):
            yield

    def get(self, key: str) -> str | None:
        try:
            return self._keychain.get_password(self._service(self._read_local()), key)
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(str(exc)) from exc

    def set(self, key: str, value: str) -> None:
        self.set_many({key: value})

    @_store_operation
    def set_many(self, values: dict[str, str]) -> None:
        index = self._read_index()
        local = self._read_local()
        service = self._service(local)
        written_keys: list[str] = []
        try:
            for key, value in values.items():
                require_secret_key(key)
                self._keychain.validate_password(key, value)
            for key, value in values.items():
                self._keychain.set_password(service, key, value)
                written_keys.append(key)
        except Exception as exc:
            if isinstance(exc, SecretStoreError):
                raise
            raise SecretStoreError(str(exc)) from exc
        finally:
            if written_keys:
                self._write_local(local)
            if not set(written_keys) <= set(index):
                self._write_index(sorted(set(index) | set(written_keys)))

    @_store_operation
    def delete(self, key: str) -> None:
        index = self._read_index()
        try:
            self._keychain.delete_password(self._service(self._read_local()), key)
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(str(exc)) from exc
        if key in index:
            self._write_index([name for name in index if name != key])

    def keys(self) -> list[str]:
        return self._read_index()

    @_store_operation
    def ensure_initialized(self) -> None:
        """Persist the index and this workspace's keychain namespace.

        The span starts at the read: what is written is the index that was
        read, so a write in between would be written back over.
        """
        self._write_index(self._read_index())
        self._write_local(self._read_local())

    def _service(self, local: dict[str, Any]) -> str:
        return f"{_KEYRING_SERVICE_PREFIX}/{local['store_id']}"

    def _read_index(self) -> list[str]:
        return read_secret_index(self.location.parent)

    def _write_index(self, keys: list[str]) -> None:
        self.location.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(self.location, {"keys": keys})

    def _read_local(self) -> dict[str, Any]:
        """Read the keychain namespace this workspace's values are held under.

        A record without a namespace -- absent or unparsable -- names nothing
        this store can read, so it starts over as a new namespace, which the
        first write keeps.

        A record that exists but cannot be read is an error, not a fresh start:
        the next write would replace the namespace it names and leave the
        values there where nothing records them.
        """
        try:
            data = json.loads(self._local_index.read_text(encoding="utf-8"))
        except FileNotFoundError, ValueError:
            data = None
        except OSError as exc:
            raise SecretStoreError(str(exc)) from exc
        store_id = data.get("store_id") if isinstance(data, dict) else None
        if not isinstance(store_id, str) or not store_id:
            return {"store_id": uuid.uuid4().hex}
        return {"store_id": store_id}

    def _write_local(self, local: dict[str, Any]) -> None:
        # Replaced whole, never truncated in place: a torn record reads as one
        # without a namespace and would orphan every value held under it.
        payload = {"schema_version": 1, **local}
        atomic_write_text(
            self._local_index,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )


def read_secret_index(config_dir: Path) -> list[str]:
    """Return the key names ``config_dir``'s index lists, reading no value.

    Raises:
        SecretStoreError: When the index holds anything but the list of names:
            read as no keys, every secret would silently go missing, and a
            field beside them is where a value would end up.
    """
    path = config_dir / SECRETS_INDEX_FILENAME
    index = load_yaml_dict(path)
    raw = index.get("keys", [])
    if (
        set(index) - {"keys"}
        or not isinstance(raw, list)
        or not all(isinstance(key, str) for key in raw)
    ):
        raise SecretStoreError(f"{path} does not hold only the names of its keys.")
    return sorted(set(raw))


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
    """True when a secret store refused because it is locked."""
    try:
        from keyring.errors import KeyringLocked
    except ImportError:
        return "lock" in str(exc).lower()
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


def identity_secret_store() -> KeyringSecretStore:
    """Return the store every workspace shares: the members' secrets, and the
    connections' that belong with them, such as a service's admin token.

    Raises:
        SecretStoreError: When the OS secret store is unavailable.
    """
    return resolve_secret_store(get_identity_config_dir())
