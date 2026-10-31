"""Workspace and device identity.

A workspace identity is generated once and stored in ``state/workspace.json``;
the member CLI names the workspace it means by it, so a backend serving another
selection refuses rather than acting there. A device identity is this
machine's: it is generated on first use, stored in
``~/.guildbotics/data/device.json``, and names the machine on the records it
writes. Neither identity authenticates anything.
"""

from __future__ import annotations

import socket
import sys
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from guildbotics.utils.advisory_lock import held_lock
from guildbotics.utils.fileio import (
    atomic_write_text,
    get_machine_state_path,
    get_workspace_state_path,
)
from guildbotics.utils.shared_writes import (
    SHARED_RECORD_SCHEMA_VERSION,
    dump_shared_json,
    update_shared_json,
)
from guildbotics.utils.timestamps import parse_iso_datetime, utc_now_iso

DeviceOs = Literal["macos", "windows", "linux"]


def _supported_schema_version(value: int) -> int:
    """Reject future generations; identity fields retain the same structure."""
    if value > SHARED_RECORD_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version must not exceed {SHARED_RECORD_SCHEMA_VERSION}, got {value}"
        )
    return value


def _uuid_string(value: str) -> str:
    uuid.UUID(value)
    return value


def _timestamp_string(value: str) -> str:
    if parse_iso_datetime(value) is None:
        raise ValueError(f"is not an ISO 8601 timestamp: {value!r}")
    return value


SchemaVersion = Annotated[int, Field(ge=1), AfterValidator(_supported_schema_version)]
UuidString = Annotated[str, AfterValidator(_uuid_string)]
TimestampString = Annotated[str, AfterValidator(_timestamp_string)]


class SharedRecord(BaseModel):
    """Base for the identity records.

    Unknown fields are rejected rather than carried along, so a record in
    another shape is refused instead of being read as this one.
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


def new_uuid7() -> str:
    """Return a UUID version 7, so directory listings of records stay roughly
    chronological."""
    return str(uuid.uuid7())


def current_device_os() -> DeviceOs:
    """Return this machine's OS as the device identity names it."""
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
    different identifier while only one survived on disk. Deciding inside the
    same span that writes is what makes that true -- the identifier already on
    disk always wins.
    """
    existing = read_workspace_identity(workspace_root)
    if existing is not None:
        return existing

    def _created(current: Any | None) -> dict[str, Any]:
        if current is not None:
            return current
        return WorkspaceIdentity(
            workspace_id=new_uuid7(), created_at=utc_now_iso()
        ).model_dump()

    return WorkspaceIdentity.model_validate(
        update_shared_json(
            workspace_identity_path(workspace_root),
            _created,
            workspace_root=workspace_root,
        )
    )


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

    The display name is the host name, so setup never has to ask for one.
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


def _device_identity_lock_path() -> Path:
    """Return the machine-wide lock guarding device identity creation."""
    return get_machine_state_path("run", "device-identity.lock")


def _write_device_identity(identity: DeviceIdentity) -> None:
    """Write this machine's identity, which lives outside every workspace."""
    atomic_write_text(device_identity_path(), dump_shared_json(identity.model_dump()))


def _default_display_name() -> str:
    return socket.gethostname().split(".")[0] or "GuildBotics device"
