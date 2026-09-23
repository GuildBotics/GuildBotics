"""AI CLI logins sealed on this device, never kept as plain files.

A brokered tool's login (the files its login command leaves) is sealed with
AES-GCM under one key this device keeps in the OS keychain. The sealed file
names what it holds -- tool, account, and credentials format -- as the
authenticated data, so a record is never opened as another tool's or another
shape's. It is written whole or not at all.

Nothing falls back to a plain file: a keychain that is locked or missing,
a key that is gone, or a record that does not open stops the tool with the
reason, and logging in again is the way back. The login is this device's
own; it is not a workspace secret and never travels to another machine.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from guildbotics.utils.advisory_lock import (
    held_lock,
    lock_file_nonblocking,
    open_lock_file,
    process_lock,
    unlock_file,
)
from guildbotics.utils.fileio import atomic_write_bytes, get_machine_state_path
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.keychain import Keychain, SecretStoreError, system_keychain

_KEYCHAIN_SERVICE = "GuildBotics"
_KEYCHAIN_ACCOUNT = "agent-environment-credentials"
_VERSION = 1
_KEY_BYTES = 32
_NONCE_BYTES = 12
_LOCK_POLL_SECONDS = 0.1

#: What a sealed login is on this device. Every state but ``saved`` stops
#: the tool, and each says what to do in :func:`vault_problem`.
VaultState = Literal["saved", "missing", "locked", "unavailable", "corrupt"]


class CredentialVaultError(RuntimeError):
    """A sealed login that cannot be read or written, and why."""

    def __init__(self, state: VaultState, detail: str = "") -> None:
        super().__init__(detail or state)
        self.state: VaultState = state


def seal(path: Path, label: str, files: Mapping[str, bytes]) -> None:
    """Seal ``files`` at ``path`` under the device key, replacing it whole.

    Args:
        path: The sealed record.
        label: What the record holds (tool, account, format); it must be the
            same to open it.
        files: The login's files, by their name under the tool's state root.

    Raises:
        CredentialVaultError: When the keychain cannot give or keep the key,
            or the record cannot be written.
    """
    key = _key(create=True)
    assert key is not None
    nonce = os.urandom(_NONCE_BYTES)
    plaintext = json.dumps(
        {name: base64.b64encode(data).decode() for name, data in files.items()},
        sort_keys=True,
    ).encode()
    record = {
        "version": _VERSION,
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(
            AESGCM(key).encrypt(nonce, plaintext, _aad(label))
        ).decode(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_bytes(path, json.dumps(record).encode())
    except OSError as exc:
        raise CredentialVaultError("unavailable", str(exc)) from exc


def unseal(path: Path, label: str) -> dict[str, bytes] | None:
    """The files sealed at ``path``, or None when nothing is sealed there.

    Raises:
        CredentialVaultError: ``locked`` / ``unavailable`` when the keychain
            cannot give the key, ``corrupt`` when the key is gone or the
            record does not open as ``label``.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CredentialVaultError("unavailable", str(exc)) from exc
    key = _key(create=False)
    if key is None:
        raise CredentialVaultError("corrupt", "the device key is missing")
    try:
        record = json.loads(raw)
        if record.get("version") != _VERSION:
            raise ValueError("unknown version")
        plaintext = AESGCM(key).decrypt(
            base64.b64decode(record["nonce"], validate=True),
            base64.b64decode(record["ciphertext"], validate=True),
            _aad(label),
        )
        files = json.loads(plaintext)
        return {
            str(name): base64.b64decode(data, validate=True)
            for name, data in files.items()
        }
    except (
        InvalidTag,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        binascii.Error,
    ) as exc:
        raise CredentialVaultError("corrupt", type(exc).__name__) from exc


def vault_problem(state: VaultState, *, tool: str, command: str) -> str:
    """Why a tool in ``state`` cannot start, in the words every caller shows."""
    values = {"tool": tool, "command": command}
    if state == "missing":
        return t("intelligences.agent_environment.tool.credentials_missing", **values)
    if state == "locked":
        return t("intelligences.agent_environment.tool.credentials_locked", **values)
    if state == "unavailable":
        return t(
            "intelligences.agent_environment.tool.credentials_unavailable", **values
        )
    if state == "corrupt":
        return t("intelligences.agent_environment.tool.credentials_corrupt", **values)
    return ""


class HeldVaultLock:
    """This process's hold on a sealed record; see :func:`held_vault_lock`."""

    def __init__(self, mutex: threading.Lock, handle: IO[str]) -> None:
        self._mutex = mutex
        self._handle: IO[str] | None = handle

    def release(self) -> None:
        """Let the next holder in; idempotent, and it never waits."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            unlock_file(handle)
        finally:
            handle.close()
            self._mutex.release()


async def held_vault_lock(path: Path, *, timeout: float) -> HeldVaultLock:
    """Hold the record at ``path`` against every thread and process here.

    Whoever might refresh the login holds this for as long as the tool can
    refresh it, so two refreshes never spend the same refresh token and a
    refreshed login is sealed before the next one reads the record. As with
    :func:`~guildbotics.utils.advisory_lock.held_lock`, the process's mutex
    comes first and the OS lock second; both are polled, never waited on, so
    the event loop runs meanwhile. The release is synchronous, so a hold can
    end in a callback that cannot wait.

    Raises:
        CredentialVaultError: ``unavailable`` when it stays held past
            ``timeout`` seconds.
    """
    lock_path = path.with_name(path.name + ".lock")
    mutex = process_lock(lock_path)
    deadline = time.monotonic() + timeout
    while not mutex.acquire(blocking=False):
        await _wait_until(deadline)
    try:
        handle = open_lock_file(lock_path)
        try:
            while True:
                try:
                    lock_file_nonblocking(handle)
                    return HeldVaultLock(mutex, handle)
                except BlockingIOError:
                    await _wait_until(deadline)
        except BaseException:
            handle.close()
            raise
    except BaseException:
        mutex.release()
        raise


async def _wait_until(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise CredentialVaultError(
            "unavailable", "another use of the login is still running"
        )
    await asyncio.sleep(_LOCK_POLL_SECONDS)


def _aad(label: str) -> bytes:
    return f"guildbotics.agent-credentials.v{_VERSION}\0{label}".encode()


def _key(*, create: bool) -> bytes | None:
    """This device's sealing key, made on first use when ``create``.

    It is made under a device-wide lock: two tools sealing their first login
    at once must not each make a key, the second replacing the first's.
    """
    from keyring.errors import KeyringError, KeyringLocked

    keychain = system_keychain()
    try:
        if (key := _stored_key(keychain)) is not None or not create:
            return key
        with held_lock(get_machine_state_path("agent_environment", "key.lock")):
            if (key := _stored_key(keychain)) is not None:
                return key
            key = AESGCM.generate_key(bit_length=_KEY_BYTES * 8)
            keychain.set_password(
                _KEYCHAIN_SERVICE, _KEYCHAIN_ACCOUNT, base64.b64encode(key).decode()
            )
            return key
    except KeyringLocked as exc:
        raise CredentialVaultError("locked", str(exc)) from exc
    except (KeyringError, SecretStoreError, OSError) as exc:
        raise CredentialVaultError("unavailable", str(exc)) from exc


def _stored_key(keychain: Keychain) -> bytes | None:
    stored = keychain.get_password(_KEYCHAIN_SERVICE, _KEYCHAIN_ACCOUNT)
    if not stored:
        return None
    try:
        key = base64.b64decode(stored, validate=True)
    except binascii.Error:
        key = b""
    if len(key) != _KEY_BYTES:
        raise CredentialVaultError("corrupt", "the device key is malformed")
    return key
