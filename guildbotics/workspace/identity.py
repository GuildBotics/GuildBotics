"""Workspace and device identity used to synchronize across machines.

A workspace identity is shared: it is generated once, stored in
``state/workspace.json``, and travels with every copy so a hub can refuse to
mix two workspaces. A device identity is machine-local: it is generated on
first use, stored in ``~/.guildbotics/data/device.json``, and published into
the shared ``state/devices/`` directory so other machines can name the device
that made a change. Neither identity authenticates anything; OpenSSH does that.
"""

from __future__ import annotations

import secrets
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from guildbotics.utils.advisory_lock import held_lock
from guildbotics.utils.fileio import (
    atomic_write_text,
    get_machine_state_path,
    get_workspace_local_path,
    get_workspace_state_path,
)
from guildbotics.utils.timestamps import parse_iso_datetime, utc_now_iso
from guildbotics.utils.workspace_sync_port import (
    SHARED_RECORD_SCHEMA_VERSION,
    dump_shared_json,
    write_shared_json,
)

DeviceOs = Literal["macos", "windows", "linux"]
DeviceStatus = Literal["active", "retired"]


def _current_schema_version(value: int) -> int:
    """Reject a record written by a schema the running code does not implement.

    Shared records switch straight to a new schema without a compatibility
    path, so reading a version this build does not know is shared-data damage,
    not something to interpret loosely.
    """
    if value != SHARED_RECORD_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version must be {SHARED_RECORD_SCHEMA_VERSION}, got {value}"
        )
    return value


def _uuid_string(value: str) -> str:
    uuid.UUID(value)
    return value


def _timestamp_string(value: str) -> str:
    if parse_iso_datetime(value) is None:
        raise ValueError(f"is not an ISO 8601 timestamp: {value!r}")
    return value


SchemaVersion = Annotated[int, AfterValidator(_current_schema_version)]
UuidString = Annotated[str, AfterValidator(_uuid_string)]
TimestampString = Annotated[str, AfterValidator(_timestamp_string)]


class SharedRecord(BaseModel):
    """Base for records that travel between devices.

    Unknown fields are rejected rather than carried along, which is what keeps
    device-local data -- paths, PIDs, cache locations, secrets -- out of shared
    files without maintaining a list of forbidden field names.
    """

    model_config = ConfigDict(extra="forbid")


class WorkspaceIdentity(SharedRecord):
    """The shared identity of one GuildBotics workspace (``state/workspace.json``)."""

    schema_version: SchemaVersion = Field(default=SHARED_RECORD_SCHEMA_VERSION)
    workspace_id: UuidString
    created_at: TimestampString


class DeviceIdentity(SharedRecord):
    """This machine's identity (``~/.guildbotics/data/device.json``, never shared)."""

    schema_version: SchemaVersion = Field(default=SHARED_RECORD_SCHEMA_VERSION)
    device_id: UuidString
    display_name: str = Field(min_length=1)
    os: DeviceOs


class DeviceRecord(SharedRecord):
    """A device as the other machines see it (``state/devices/<device_id>.json``).

    Online state, tool versions, and running jobs come from a live session and
    are deliberately absent here, so this record changes only when the user
    renames a device, joins one, or retires one.
    """

    schema_version: SchemaVersion = Field(default=SHARED_RECORD_SCHEMA_VERSION)
    device_id: UuidString
    display_name: str = Field(min_length=1)
    os: DeviceOs
    joined_at: TimestampString
    status: DeviceStatus = "active"
    ssh_public_key_fingerprint: str | None = None


def new_uuid7() -> str:
    """Return a UUID version 7: a 48-bit millisecond timestamp plus randomness.

    Time-ordered identifiers keep directory listings of shared records roughly
    chronological, which the standard library gains only in Python 3.14.
    """
    value = (int(time.time() * 1000) & 0xFFFF_FFFF_FFFF) << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return str(uuid.UUID(int=value))


def current_device_os() -> DeviceOs:
    """Return this machine's OS as the shared device records name it."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def workspace_identity_path(workspace_root: Path | None = None) -> Path:
    """Return ``<workspace>/.guildbotics/state/workspace.json``."""
    return get_workspace_state_path("workspace.json", workspace_root=workspace_root)


def read_workspace_identity(
    workspace_root: Path | None = None,
) -> WorkspaceIdentity | None:
    """Return the workspace identity, or None when it has not been created yet."""
    path = workspace_identity_path(workspace_root)
    if not path.exists():
        return None
    return WorkspaceIdentity.model_validate_json(path.read_text(encoding="utf-8"))


def ensure_workspace_identity(
    workspace_root: Path | None = None,
) -> WorkspaceIdentity:
    """Return the workspace identity, creating it on first use.

    The identity is generated exactly once per workspace, so first use is
    serialized: two processes starting together would otherwise each return a
    different identifier while only one survived on disk, and a caller holding
    the discarded one would register the wrong workspace with a hub. Every
    later copy adopts the value it received, so joining a hub never renumbers
    a workspace.
    """
    existing = read_workspace_identity(workspace_root)
    if existing is not None:
        return existing
    lock_path = get_workspace_local_path(
        "run", "workspace-identity.lock", workspace_root=workspace_root
    )
    with held_lock(lock_path):
        existing = read_workspace_identity(workspace_root)
        if existing is not None:
            return existing
        identity = WorkspaceIdentity(workspace_id=new_uuid7(), created_at=utc_now_iso())
        write_shared_json(
            workspace_identity_path(workspace_root),
            identity.model_dump(),
            workspace_root=workspace_root,
        )
        return identity


def device_identity_path() -> Path:
    """Return ``~/.guildbotics/data/device.json``."""
    return get_machine_state_path("device.json")


def read_device_identity() -> DeviceIdentity | None:
    """Return this machine's identity, or None when it has not been created yet."""
    path = device_identity_path()
    if not path.exists():
        return None
    return DeviceIdentity.model_validate_json(path.read_text(encoding="utf-8"))


def ensure_device_identity() -> DeviceIdentity:
    """Return this machine's identity, creating it on first use.

    Like the workspace identity, first use is serialized so that concurrent
    starts cannot hand two processes different device identifiers.

    The initial display name is the host name so setup never has to ask for
    one; the user can rename the device later from the sync settings screen.
    """
    existing = read_device_identity()
    if existing is not None:
        return existing
    with held_lock(_device_identity_lock_path()):
        existing = read_device_identity()
        if existing is not None:
            return existing
        identity = DeviceIdentity(
            device_id=new_uuid7(),
            display_name=_default_display_name(),
            os=current_device_os(),
        )
        _write_device_identity(identity)
        return identity


def set_device_display_name(display_name: str) -> DeviceIdentity:
    """Rename this machine, keeping its identifier.

    Args:
        display_name (str): The new name. Surrounding whitespace is trimmed.

    Raises:
        ValueError: When ``display_name`` is blank.
    """
    name = display_name.strip()
    if not name:
        raise ValueError("A device display name must not be blank.")
    ensure_device_identity()
    with held_lock(_device_identity_lock_path()):
        current = read_device_identity()
        assert current is not None  # ensured above, and only ever created
        identity = current.model_copy(update={"display_name": name})
        _write_device_identity(identity)
        return identity


def device_record_path(device_id: str, workspace_root: Path | None = None) -> Path:
    """Return ``<workspace>/.guildbotics/state/devices/<device_id>.json``."""
    return get_workspace_state_path(
        "devices", f"{device_id}.json", workspace_root=workspace_root
    )


def publish_device_record(
    workspace_root: Path | None = None,
    ssh_public_key_fingerprint: str | None = None,
) -> DeviceRecord:
    """Publish this machine into the workspace's shared device list.

    Rewriting an unchanged record would create pointless synchronization work,
    so an identical record is left alone.

    Args:
        workspace_root (Path | None): The workspace, or None to use the selected one.
        ssh_public_key_fingerprint (str | None): The key registered with the hub,
            or None to keep the fingerprint already published.
    """
    identity = ensure_device_identity()
    path = device_record_path(identity.device_id, workspace_root)
    existing = read_device_record(identity.device_id, workspace_root)
    record = DeviceRecord(
        device_id=identity.device_id,
        display_name=identity.display_name,
        os=identity.os,
        joined_at=existing.joined_at if existing is not None else utc_now_iso(),
        status=existing.status if existing is not None else "active",
        ssh_public_key_fingerprint=(
            ssh_public_key_fingerprint
            if ssh_public_key_fingerprint is not None
            else (existing.ssh_public_key_fingerprint if existing else None)
        ),
    )
    if record != existing:
        write_shared_json(path, record.model_dump(), workspace_root=workspace_root)
    return record


def read_device_record(
    device_id: str, workspace_root: Path | None = None
) -> DeviceRecord | None:
    """Return one shared device record, or None when the device is unknown."""
    path = device_record_path(device_id, workspace_root)
    if not path.exists():
        return None
    return DeviceRecord.model_validate_json(path.read_text(encoding="utf-8"))


def list_device_records(workspace_root: Path | None = None) -> list[DeviceRecord]:
    """Return every shared device record, ordered by device identifier."""
    root = get_workspace_state_path("devices", workspace_root=workspace_root)
    if not root.is_dir():
        return []
    records = [
        DeviceRecord.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(root.glob("*.json"))
    ]
    return sorted(records, key=lambda record: record.device_id)


def _device_identity_lock_path() -> Path:
    """Return the machine-wide lock guarding device identity creation."""
    return get_machine_state_path("run", "device-identity.lock")


def _write_device_identity(identity: DeviceIdentity) -> None:
    """Write the machine-local identity, which is never part of shared state."""
    atomic_write_text(device_identity_path(), dump_shared_json(identity.model_dump()))


def _default_display_name() -> str:
    return socket.gethostname().split(".")[0] or "GuildBotics device"
