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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from guildbotics.utils.fileio import (
    get_workspace_config_dir,
    load_yaml_dict,
    save_yaml_file,
)
from guildbotics.utils.keychain import (
    InvalidSecretKeyError,
    Keychain,
    SecretStoreError,
    system_keychain,
)
from guildbotics.utils.shared_write_lock import shared_write_operation

SECRETS_INDEX_FILENAME = "secrets.yml"
LOCAL_SECRETS_FILENAME = "secrets.json"
#: The generation of a key that exists but whose value has never reached the
#: hub. Shared generations count the values every device can obtain, so a value
#: only this machine holds is not one of them yet -- and a workspace that never
#: connects to a hub simply keeps every key here.
UNSHARED_GENERATION = 0
_KEYRING_SERVICE_PREFIX = "GuildBotics"
_PLAIN_ENV_VALUE = re.compile(r"[^\s#'\"\\]*")
#: What a logical key may be called. A key reaches another machine as one
#: argument of the hub's own command, and OpenSSH hands that command line to a
#: shell there, so a name is held to what cannot be read as anything but a
#: single word. Refusing the rest where a key is created is what keeps a key
#: from existing that could never be transferred.
#:
#: A leading digit is allowed. Member keys are named after the member, and a
#: ``person_id`` may start with one; the property wanted here is that a shell
#: finds nothing to do with the name, not that it would pass as an identifier.
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
    workspace SecretStore are secret whatever they are called. Used both to
    keep such values out of AI CLI subprocess environments and to redact them
    from anything a member writes down, so the two answers cannot drift apart.
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


class SecretKeyStatus(StrEnum):
    """What one device can say about one key without contacting the hub.

    Each is decided by comparing two numbers that are already recorded -- the
    generation every device agreed on and the generation this machine holds --
    and one flag saying whether the value here has been given to the hub.

    Nothing here records a transfer in progress. A send that was cut off part
    way leaves its evidence on the hub, where every device can see it, rather
    than in a note on the one machine that was sending: such a note does not
    survive the value being entered again, and does not survive that machine.
    """

    #: This machine holds the generation the other devices agreed on.
    READY = "ready"
    #: The key is known, but this machine has no value for it.
    MISSING = "missing"
    #: Another device published a newer generation than this machine holds.
    OUTDATED = "outdated"
    #: A value entered here has not reached the hub yet.
    PENDING_SEND = "pending_send"
    #: A value entered here, and the shared generation moved past the one it
    #: was derived from -- two machines changed the same key.
    CONFLICT = "conflict"


@dataclass(frozen=True)
class SecretKeyState:
    """One key as this device sees it.

    Attributes:
        key (str): The logical key name.
        status (SecretKeyStatus): What the user has to do about it, if anything.
        shared_generation (int): The generation every device agreed on, or
            :data:`UNSHARED_GENERATION` when no value has reached the hub.
        local_generation (int | None): The generation this machine's value
            belongs to, or None when it holds no value.
        pending_send (bool): Whether a value entered here still has to be sent.
        updated_at (str): When the shared generation was published.
    """

    key: str
    status: SecretKeyStatus
    shared_generation: int
    local_generation: int | None
    pending_send: bool
    updated_at: str


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
    """OS keychain storage with a non-secret key index in the workspace.

    Every change here reads the shared index, moves values in the keychain, and
    writes the index back, so the whole operation is one span rather than the
    write at the end of it: read outside one, the index that gets written back
    is the one from before a hub's version was adopted, and the generations
    another device published are silently reverted. The operations declare that
    span with :func:`~guildbotics.utils.shared_write_lock.shared_write_operation`
    because it is simply the method -- there is nothing in them that waits on
    anything remote.
    """

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
        state = self.key_state(key)
        if state is None or state.status in _WITHHELD_STATUSES:
            # Either this device holds nothing, or another device published a
            # newer generation and what this keychain holds is outdated. Never
            # serve the second -- the key needs a fetch, or a `secrets set`
            # here (`secrets status` lists it).
            return None
        try:
            return self._keychain.get_password(self._service(self._read_index()), key)
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(str(exc)) from exc

    def stale_keys(self) -> list[str]:
        """Keys whose shared generation this device does not hold."""
        return sorted(
            state.key
            for state in self.key_states()
            if state.status is SecretKeyStatus.OUTDATED
        )

    def key_states(self) -> list[SecretKeyState]:
        """Return every key this device knows of, from either record.

        A key named by only one of the two files is still a key. The shared
        index can lose an entry -- a first-committer-wins race can set aside the
        very commit that created it -- and the value would then be held here
        under a name nothing lists, which is the one way a key becomes
        impossible to act on. Reading both records keeps it visible, and
        sending it puts the shared entry back.
        """
        index = self._read_index()
        local = self._read_local()["keys"]
        return [
            _key_state(key, index["keys"].get(key), local.get(key))
            for key in sorted(set(index["keys"]) | set(local))
        ]

    def key_state(self, key: str) -> SecretKeyState | None:
        """Return one key's state, or None when neither record names it."""
        index = self._read_index()
        local = self._read_local()["keys"].get(key)
        meta = index["keys"].get(key)
        if meta is None and local is None:
            return None
        return _key_state(key, meta, local)

    def set(self, key: str, value: str) -> None:
        self.set_many({key: value})

    @shared_write_operation
    def set_many(self, values: dict[str, str]) -> None:
        index = self._read_index()
        service = self._service(index)
        written_keys: list[str] = []
        try:
            for key, value in values.items():
                require_secret_key(key)
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
    def confirm_shared(
        self, generations: dict[str, int], *, sent: dict[str, str]
    ) -> None:
        """Publish these generations as the ones every device is to hold.

        Called once the hub holds this device's values: the shared index is
        what tells the other machines there is something newer to fetch, so it
        is written after the values are safely on the hub and never before.

        ``sent`` names the value that actually reached the hub for each key.
        The exchange with the hub is not inside this store's lock -- nothing
        is, across a network -- so a value can be entered here while the send
        is in flight. The hub-side fact still holds either way: the generation
        is published. What is *not* written is the local claim that this
        machine holds it, unless the value here is still the one that was
        sent; a value typed in between stays recorded as one that has not
        been shared, because it has not.

        A whole transfer's keys are published in one span. The span is a local
        read-modify-write with nothing remote inside it -- the exchange with
        the hub is already over -- so it never makes another writer wait on a
        network round trip.
        """
        if not generations:
            return
        index = self._read_index()
        service = self._service(index)
        now = utc_now_iso()
        local = self._read_local()
        for key, generation in generations.items():
            if key not in sent:
                # A generation for a key this device never offered can only be
                # a corrupted answer; publishing it would name a value nobody
                # here vouched for.
                continue
            current = _as_generation((index["keys"].get(key) or {}).get("generation"))
            # A shared generation only ever moves forward. Two devices can each
            # reach the hub and publish, and the one whose answer comes back
            # later would otherwise write the earlier number over the later
            # one -- putting a completed, recorded send back into the state
            # that says it never finished.
            if current is not None and current >= generation:
                continue
            index["keys"][key] = {"generation": generation, "updated_at": now}
            if self._holds_value(service, key, sent[key]):
                local["keys"][key] = {"generation": generation, "pending_send": False}
        self._write_index(index)
        self._write_local(local)

    def _holds_value(self, service: str, key: str, sent_value: str) -> bool:
        """Whether this machine's value for ``key`` is still ``sent_value``.

        A keychain that cannot answer counts as "no": the safe wrong answer
        leaves the key marked as still to be sent, and the next send settles
        it either way.
        """
        try:
            return self._keychain.get_password(service, key) == sent_value
        except Exception:
            return False

    @shared_write_operation
    def adopt_received(self, key: str, value: str, generation: int) -> None:
        """Store a value taken from the hub as the generation the hub holds.

        The shared index is left alone: the generation being adopted is already
        published -- fetching is how this machine catches up with it -- and
        writing it again here would make every fetch look like a change to the
        other devices.
        """
        index = self._read_index()
        try:
            require_secret_key(key)
            self._keychain.validate_password(key, value)
            self._keychain.set_password(self._service(index), key, value)
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(str(exc)) from exc
        local = self._read_local()
        local["keys"][key] = {"generation": generation, "pending_send": False}
        self._write_local(local)

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
        require_secret_key(new_key)
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
        return [state.key for state in self.key_states()]

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
        """Record locally entered values as this device's own, unsent update.

        Entering a value does not advance the shared generation. That number
        names a value every device can obtain, and until the hub holds this one
        no other machine can: raising it here would tell them to fetch
        something that is not there, and -- when two machines were edited at
        once -- give two different values the same number, which nothing
        downstream could tell apart.

        """
        now = utc_now_iso()
        changed = False
        local = self._read_local()
        for key in keys:
            if key not in index["keys"]:
                index["keys"][key] = {
                    "generation": UNSHARED_GENERATION,
                    "updated_at": now,
                }
                changed = True
            shared = _as_generation(index["keys"][key].get("generation"))
            local["keys"][key] = {
                "generation": UNSHARED_GENERATION if shared is None else shared,
                "pending_send": True,
            }
        if changed:
            self._write_index(index)
        self._write_local(local)

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
            "locked": is_locked_error(exc),
            "backend": "os-keychain",
        }
    return {"available": True, "locked": False, "backend": "os-keychain"}


def is_locked_error(exc: BaseException) -> bool:
    """True when a secret store refused because it is locked.

    The one answer to that question. Every place that has to tell "locked" from
    "unavailable" -- this device, the hub's own command, the transfer that
    reports it -- asks here, so the three cannot start disagreeing.
    """
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
    """Read a generation counter, or None when the field is not one.

    ``0`` is a generation like any other: it is the one a key has before any
    device has managed to share its value.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < UNSHARED_GENERATION:
        return None
    return value


#: Statuses where the value this machine holds is not the one to use: either
#: there is none, or another device published a newer one that has to be
#: fetched before anything reads this key again.
_WITHHELD_STATUSES = frozenset({SecretKeyStatus.MISSING, SecretKeyStatus.OUTDATED})


def _key_state(
    key: str, meta: dict[str, Any] | None, local: dict[str, Any] | None
) -> SecretKeyState:
    """Decide one key's state from the shared index and the local record."""
    meta = meta or {}
    shared = _as_generation(meta.get("generation")) or UNSHARED_GENERATION
    updated_at = str(meta.get("updated_at") or "")
    if local is None:
        return SecretKeyState(
            key=key,
            status=SecretKeyStatus.MISSING,
            shared_generation=shared,
            local_generation=None,
            pending_send=False,
            updated_at=updated_at,
        )
    held = _as_generation(local.get("generation"))
    pending = bool(local.get("pending_send", False))
    if pending:
        status = (
            SecretKeyStatus.CONFLICT
            if held is not None and held < shared
            else SecretKeyStatus.PENDING_SEND
        )
    elif held is None or held < shared:
        status = SecretKeyStatus.OUTDATED
    else:
        status = SecretKeyStatus.READY
    return SecretKeyState(
        key=key,
        status=status,
        shared_generation=shared,
        local_generation=held,
        pending_send=pending,
        updated_at=updated_at,
    )
