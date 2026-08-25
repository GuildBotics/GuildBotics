"""File-backed Hub relay state for live snapshots and service ownership.

This module only knows the Hub's directory layout and the transport-neutral
file operations. It deliberately does not import workspace models or inspect
the meaning of a live snapshot; the device side owns that contract.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from guildbotics.hub import host
from guildbotics.utils.fileio import atomic_write_text
from guildbotics.utils.timestamps import utc_now_iso
from guildbotics.utils.workspace_sync_port import (
    SHARED_RECORD_SCHEMA_VERSION,
    dump_shared_json,
)

SERVICE_OWNER_SCHEMA_VERSION = 1
LIVE_EXPIRE_AFTER_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 1.0
_RELAY_PATH_PARTS = 2
HEAD_UPDATED_EVENT = '{"kind":"head-updated"}'
LIVE_EXPIRED_EVENT_KIND = "live-expired"


class HubRelayError(RuntimeError):
    """Raised when relay state cannot be read or written safely."""


class ServiceOwner(BaseModel):
    """The one persistent service owner for a Hub workspace."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=SERVICE_OWNER_SCHEMA_VERSION)
    workspace_id: str
    owner_device_id: str
    updated_at: str


@dataclass(frozen=True)
class RelayPoll:
    """One poll's output and the cursor used by the next poll."""

    lines: tuple[str, ...]
    live_cursor: dict[str, tuple[int, int]]
    head_cursor: tuple[int, int] | None


def workspace_relay_root(workspace_id: str) -> Path:
    """Return the Hub directory containing one workspace's relay files."""
    return host.workspace_repository_path(
        host.require_workspace_id(workspace_id)
    ).parent


def service_owner_path(workspace_id: str) -> Path:
    return workspace_relay_root(workspace_id) / "service-owner.json"


def live_root(workspace_id: str) -> Path:
    return workspace_relay_root(workspace_id) / "live"


def head_updated_path(workspace_id: str) -> Path:
    return workspace_relay_root(workspace_id) / "head-updated"


def read_service_owner(workspace_id: str) -> ServiceOwner | None:
    canonical_workspace_id = host.require_workspace_id(workspace_id)
    path = service_owner_path(canonical_workspace_id)
    if not path.is_file():
        return None
    try:
        owner = ServiceOwner.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        raise HubRelayError(f"The service owner file is invalid: {path}") from exc
    if owner.workspace_id != canonical_workspace_id:
        raise HubRelayError(f"The service owner file names the wrong workspace: {path}")
    return owner


def claim_service_owner(workspace_id: str, device_id: str) -> tuple[ServiceOwner, bool]:
    """Create the owner file only when it does not exist already."""
    workspace_id = host.require_workspace_id(workspace_id)
    _require_relay_uuid(device_id, "device_id")
    path = service_owner_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = ServiceOwner(
        workspace_id=host.require_workspace_id(workspace_id),
        owner_device_id=device_id,
        updated_at=utc_now_iso(),
    )
    data = dump_shared_json(owner.model_dump())
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        existing = read_service_owner(workspace_id)
        if existing is None:  # pragma: no cover - a concurrent delete is damage
            raise HubRelayError(f"The service owner file disappeared: {path}") from None
        return existing, False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return owner, True


def transfer_service_owner(workspace_id: str, device_id: str) -> ServiceOwner:
    """Atomically replace the owner after an explicit user transfer."""
    workspace_id = host.require_workspace_id(workspace_id)
    _require_relay_uuid(device_id, "device_id")
    path = service_owner_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = ServiceOwner(
        workspace_id=host.require_workspace_id(workspace_id),
        owner_device_id=device_id,
        updated_at=utc_now_iso(),
    )
    atomic_write_text(path, dump_shared_json(owner.model_dump()))
    return owner


def publish_live_line(
    workspace_id: str,
    device_id: str,
    publisher_id: str,
    line: str,
) -> Path:
    """Atomically replace one publisher's opaque snapshot line."""
    workspace_id = host.require_workspace_id(workspace_id)
    _require_relay_uuid(device_id, "device_id")
    _require_relay_uuid(publisher_id, "publisher_id")
    value = line.rstrip("\r\n")
    if not value:
        raise HubRelayError("A live snapshot cannot be empty.")
    path = live_root(workspace_id) / device_id / f"{publisher_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, value + "\n")
    return path


def publish_live_lines(
    workspace_id: str,
    device_id: str,
    publisher_id: str,
    lines: Iterable[str],
) -> Path | None:
    """Consume stdin-like lines until disconnect, retaining the last path."""
    published: Path | None = None
    for line in lines:
        if line.strip():
            published = publish_live_line(workspace_id, device_id, publisher_id, line)
    return published


def poll_live(
    workspace_id: str,
    *,
    live_cursor: dict[str, tuple[int, int]] | None = None,
    head_cursor: tuple[int, int] | None = None,
    now: datetime | None = None,
    expire_after: float = LIVE_EXPIRE_AFTER_SECONDS,
) -> RelayPoll:
    """Poll live files and the head marker once.

    Live contents are forwarded unchanged. The only field Hub reads is the
    timestamp needed to remove an expired file; malformed snapshots are kept
    and forwarded so the device can report a schema error rather than losing
    evidence at the relay.
    """
    workspace_id = host.require_workspace_id(workspace_id)
    current = now or datetime.now(UTC)
    previous_live = live_cursor or {}
    next_live: dict[str, tuple[int, int]] = {}
    lines: list[str] = []
    root = live_root(workspace_id)
    if root.is_dir():
        for path in sorted(root.glob("*/*.json")):
            if not path.is_file():
                continue
            key = path.relative_to(root).as_posix()
            try:
                stat = path.stat()
                marker = (stat.st_mtime_ns, stat.st_size)
                value = path.read_text(encoding="utf-8").rstrip("\r\n")
            except OSError:
                continue
            if _expired(value, current, expire_after):
                expired_event = _expired_event(workspace_id, key, value)
                if expired_event is not None:
                    lines.append(expired_event)
                with suppress(OSError):
                    path.unlink(missing_ok=True)
                continue
            next_live[key] = marker
            if previous_live.get(key) != marker and value:
                lines.append(value)

    marker_path = head_updated_path(workspace_id)
    next_head: tuple[int, int] | None = None
    try:
        stat = marker_path.stat()
    except OSError:
        pass
    else:
        next_head = (stat.st_mtime_ns, stat.st_size)
        if next_head != head_cursor:
            lines.append(HEAD_UPDATED_EVENT)
    return RelayPoll(tuple(lines), next_live, next_head)


def watch_live(
    workspace_id: str,
    *,
    output: Any,
    stop_event: Event | None = None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    expire_after: float = LIVE_EXPIRE_AFTER_SECONDS,
) -> None:
    """Poll until the caller closes the connection or asks it to stop."""
    workspace_id = host.require_workspace_id(workspace_id)
    live_cursor: dict[str, tuple[int, int]] = {}
    head_cursor: tuple[int, int] | None = None
    stop = stop_event or Event()
    while not stop.is_set():
        result = poll_live(
            workspace_id,
            live_cursor=live_cursor,
            head_cursor=head_cursor,
            expire_after=expire_after,
        )
        live_cursor = result.live_cursor
        head_cursor = result.head_cursor
        for line in result.lines:
            output.write(line + "\n")
            output.flush()
        stop.wait(poll_interval)


def _expired(value: str, now: datetime, expire_after: float) -> bool:
    try:
        payload = json.loads(value)
        observed_at = payload["observed_at"]
        timestamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return (now - timestamp.astimezone(UTC)).total_seconds() > expire_after


def _expired_event(workspace_id: str, key: str, value: str) -> str | None:
    parts = key.split("/")
    if len(parts) != _RELAY_PATH_PARTS or not parts[1].endswith(".json"):
        return None
    try:
        observed_at = str(json.loads(value)["observed_at"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return dump_shared_json(
        {
            "kind": LIVE_EXPIRED_EVENT_KIND,
            "schema_version": SHARED_RECORD_SCHEMA_VERSION,
            "workspace_id": workspace_id,
            "device_id": parts[0],
            "publisher_id": parts[1][:-5],
            "observed_at": observed_at,
        }
    ).rstrip("\n")


def _require_relay_uuid(value: str, label: str) -> str:
    try:
        return host.require_uuid(value, label)
    except host.InvalidWorkspaceIdError as exc:
        raise HubRelayError(str(exc)) from exc
