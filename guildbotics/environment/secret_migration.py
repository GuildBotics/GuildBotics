"""Explicit, offline cutover from Python keychain values to Rust's store.

This is a one-shot maintenance tool, never a runtime fallback. All servers,
CLI writers and git helpers must be stopped. Supply every workspace root.
Only macOS has a system inventory adapter; tests inject an in-memory inventory.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import plistlib
import sys
import uuid
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from guildbotics.environment.credential_vault import _key
from guildbotics.utils.fileio import atomic_write_bytes
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.keychain import Keychain, SecretStoreError, system_keychain
from guildbotics.utils.secret_store import read_secret_index, require_secret_key
from guildbotics.utils.shared_write_lock import shared_write_lock
from guildbotics.utils.shared_writes import write_shared_json

DEVICE_ENTRY = ("GuildBotics", "agent-environment-credentials")
GIT_SERVICE = "GuildBotics/branvil-git"
Entry = tuple[str, str]
_ITEM_NOT_FOUND = -25300


def git_token_key(origin: str) -> str:
    """Name an owner's per-origin token in the identity SecretStore.

    The full origin's UTF-8 SHA-256 is used without URL normalization, matching
    the legacy keychain account. Consumers at cutover must use this mapping.
    """
    return "BRANVIL_GIT_TOKEN_" + hashlib.sha256(origin.encode()).hexdigest().upper()


def macos_inventory() -> list[Entry]:
    """Enumerate generic-password attributes, never requesting password data."""
    if sys.platform != "darwin":
        raise SecretStoreError("Migration inventory currently requires macOS")
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    pointer = ctypes.c_void_p

    def constant(library: ctypes.CDLL, name: str) -> ctypes.c_void_p:
        return pointer.in_dll(library, name)

    core.CFDictionaryCreate.argtypes = [
        pointer,
        pointer,
        pointer,
        ctypes.c_long,
        pointer,
        pointer,
    ]
    core.CFDictionaryCreate.restype = pointer
    core.CFPropertyListCreateData.argtypes = [
        pointer,
        pointer,
        ctypes.c_long,
        ctypes.c_ulong,
        pointer,
    ]
    core.CFPropertyListCreateData.restype = pointer
    core.CFDataGetLength.argtypes = [pointer]
    core.CFDataGetLength.restype = ctypes.c_long
    core.CFDataGetBytePtr.argtypes = [pointer]
    core.CFDataGetBytePtr.restype = pointer
    core.CFRelease.argtypes = [pointer]
    security.SecItemCopyMatching.argtypes = [pointer, pointer]
    security.SecItemCopyMatching.restype = ctypes.c_int32
    keys = (pointer * 3)(
        *(
            constant(security, name)
            for name in ("kSecClass", "kSecMatchLimit", "kSecReturnAttributes")
        )
    )
    values = (pointer * 3)(
        constant(security, "kSecClassGenericPassword"),
        constant(security, "kSecMatchLimitAll"),
        constant(core, "kCFBooleanTrue"),
    )
    query = core.CFDictionaryCreate(None, keys, values, 3, None, None)
    result = pointer()
    try:
        status = security.SecItemCopyMatching(query, ctypes.byref(result))
        if status == _ITEM_NOT_FOUND:
            return []
        if status != 0:
            raise SecretStoreError("Keychain inventory unavailable")
        data = core.CFPropertyListCreateData(None, result, 200, 0, None)
        if not data:
            raise SecretStoreError("Keychain inventory cannot be decoded")
        try:
            items = plistlib.loads(
                ctypes.string_at(
                    core.CFDataGetBytePtr(data), core.CFDataGetLength(data)
                )
            )
        finally:
            core.CFRelease(data)
        return [
            (item["svce"], item["acct"])
            for item in items
            if isinstance(item.get("svce"), str)
            and (
                item["svce"] == "GuildBotics" or item["svce"].startswith("GuildBotics/")
            )
        ]
    finally:
        if result:
            core.CFRelease(result)
        core.CFRelease(query)


def _namespace(root: Path) -> str | None:
    path = root / ".guildbotics/local/secrets.json"
    try:
        record = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    store_id = record.get("store_id")
    if not isinstance(store_id, str) or not store_id:
        raise ValueError("Invalid namespace")
    return store_id


def _open(root: Path, store_id: str, key: bytes) -> dict[str, str] | None:
    try:
        sealed = json.loads((root / ".guildbotics/local/secrets.enc.json").read_bytes())
    except FileNotFoundError:
        return None
    if set(sealed) != {"version", "nonce", "ciphertext"} or sealed["version"] != 1:
        raise ValueError("Invalid envelope")
    plain = AESGCM(key).decrypt(
        base64.b64decode(sealed["nonce"], validate=True),
        base64.b64decode(sealed["ciphertext"], validate=True),
        f"guildbotics.secrets.v1\0{store_id}".encode(),
    )
    values = json.loads(plain)
    if not isinstance(values, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in values.items()
    ):
        raise ValueError("Invalid payload")
    for name in values:
        require_secret_key(name)
    return values


def migrate(
    identity: Path,
    workspaces: Iterable[Path],
    *,
    keychain: Keychain,
    inventory: Callable[[], list[Entry]],
    device_key: bytes,
) -> dict[str, int]:
    """Seal and verify every destination before removing any source value.

    Holds identity then workspace locks across the entire multi-store operation.
    A retry accepts identical ciphertext values and already deleted sources;
    conflicts, unknown entries and missing indexed values stop before deletion.
    Errors deliberately omit underlying backend text, which may contain values.
    """
    stage = "preflight"
    try:
        roots = [
            identity.resolve(),
            *sorted({p.resolve() for p in workspaces} - {identity.resolve()}),
        ]
        with ExitStack() as stack:
            for root in roots:
                stack.enter_context(shared_write_lock(root))
            entries = inventory()
            if len(entries) != len(set(entries)) or DEVICE_ENTRY not in entries:
                raise ValueError("Ambiguous inventory or missing device key")
            encoded = keychain.get_password(*DEVICE_ENTRY)
            if (
                encoded is None
                or base64.b64decode(encoded, validate=True) != device_key
            ):
                raise ValueError("Device key changed")
            sources: dict[Entry, str] = {}
            plans = []
            known = {DEVICE_ENTRY}
            namespaces: set[str] = set()
            for root in roots:
                names = read_secret_index(root / ".guildbotics/config")
                store_id = _namespace(root)
                if store_id is None and (
                    names or (root / ".guildbotics/local/secrets.enc.json").exists()
                ):
                    raise ValueError("Missing namespace")
                store_id = store_id or uuid.uuid4().hex
                if store_id in namespaces:
                    raise ValueError("Duplicate namespace")
                namespaces.add(store_id)
                values = _open(root, store_id, device_key) or {}
                recoverable = (
                    {
                        git_token_key(account)
                        for service, account in entries
                        if service == GIT_SERVICE
                    }
                    if root == roots[0]
                    else set()
                )
                if set(values) - set(names) - recoverable:
                    raise ValueError("Index and ciphertext disagree")
                for name in names:
                    require_secret_key(name)
                    entry = (f"GuildBotics/{store_id}", name)
                    if entry in known:
                        raise ValueError("Duplicate source namespace")
                    known.add(entry)
                    value = keychain.get_password(*entry)
                    if value is None:
                        if name not in values:
                            raise ValueError("Indexed source unavailable")
                    else:
                        if name in values and values[name] != value:
                            raise ValueError("Source and destination conflict")
                        values[name] = sources[entry] = value
                if root == roots[0]:
                    for entry in entries:
                        if entry[0] != GIT_SERVICE:
                            continue
                        known.add(entry)
                        value = keychain.get_password(*entry)
                        name = git_token_key(entry[1])
                        if value is None or (name in values and values[name] != value):
                            raise ValueError("Git token unavailable or conflicts")
                        values[name] = sources[entry] = value
                plans.append((root, store_id, values))
            if set(entries) - known:
                raise ValueError("Unmapped GuildBotics entries; supply all roots")
            stage = "destination_write_and_verification"
            for root, store_id, values in plans:
                local = root / ".guildbotics/local"
                if _namespace(root) is None:
                    atomic_write_bytes(
                        local / "secrets.json",
                        json.dumps(
                            {"schema_version": 1, "store_id": store_id}
                        ).encode(),
                    )
                nonce = os.urandom(12)
                ciphertext = AESGCM(device_key).encrypt(
                    nonce,
                    json.dumps(values, ensure_ascii=False).encode(),
                    f"guildbotics.secrets.v1\0{store_id}".encode(),
                )
                atomic_write_bytes(
                    local / "secrets.enc.json",
                    json.dumps(
                        {
                            "version": 1,
                            "nonce": base64.b64encode(nonce).decode(),
                            "ciphertext": base64.b64encode(ciphertext).decode(),
                        }
                    ).encode(),
                )
                write_shared_json(
                    root / ".guildbotics/config/secrets.yml",
                    {"keys": sorted(values)},
                    workspace_root=root,
                )
                if _open(root, store_id, device_key) != values or read_secret_index(
                    root / ".guildbotics/config"
                ) != sorted(values):
                    raise ValueError("Destination verification failed")
            stage = "source_cleanup"
            for entry, value in sources.items():
                if keychain.get_password(*entry) != value:
                    raise ValueError("Source changed; keep writers stopped")
                keychain.delete_password(*entry)
                if keychain.get_password(*entry) is not None:
                    raise ValueError("Source deletion failed")
            stage = "remaining_items_audit"
            if inventory() != [DEVICE_ENTRY]:
                raise ValueError("GuildBotics entries remain")
            return {"stores": len(plans), "removed": len(sources), "remaining": 1}
    except Exception:
        raise SecretStoreError(
            f"Secret migration stopped ({stage}); sources or verified ciphertext retained. Keep writers stopped."
        ) from None


def main() -> None:
    """Run only when explicitly invoked during the supervised cutover."""
    import argparse

    parser = argparse.ArgumentParser(description=t("cli.secret_migration.description"))
    parser.add_argument(
        "--identity", required=True, type=Path, help=t("cli.secret_migration.identity")
    )
    parser.add_argument(
        "--workspace",
        action="append",
        default=[],
        type=Path,
        help=t("cli.secret_migration.workspace"),
    )
    parser.add_argument(
        "--writers-stopped",
        required=True,
        action="store_true",
        help=t("cli.secret_migration.writers_stopped"),
    )
    args = parser.parse_args()
    try:
        key = _key(create=False)
        if key is None:
            raise SecretStoreError("Device key missing")
        result = migrate(
            args.identity,
            args.workspace,
            keychain=system_keychain(),
            inventory=macos_inventory,
            device_key=key,
        )
    except Exception:
        parser.exit(1, t("cli.secret_migration.failed") + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
