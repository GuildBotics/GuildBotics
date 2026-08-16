"""Workspace Sync Port: the notification boundary for shared workspace writes.

Everything written under ``<workspace>/.guildbotics/config`` or
``<workspace>/.guildbotics/state`` is shared across the user's machines, while
``local/`` stays on one device. Storage layers announce a completed shared
write here as a :class:`ChangeSet`. A Git Sync Manager is the only subscriber
and turns those announcements into synchronization work, so no capability,
command runner, or API layer ever learns that Git is involved.

The port lives in ``utils`` because every storage layer notifies through it,
including ``guildbotics.observability``, which may depend on ``utils`` alone.
Workspaces without synchronization keep the no-op port and pay nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from guildbotics.utils.fileio import (
    WorkspaceNotConfiguredError,
    atomic_write_bytes,
    get_workspace_root,
)

#: Directories under ``.guildbotics/`` whose contents are shared between devices.
SHARED_ROOTS = ("config", "state")

#: Shared records move to a new schema together, without a compatibility path.
#: Every record GuildBotics writes under ``state/`` stamps this, which is the
#: one thing a device receiving a newer build's file can check for itself. It
#: lives here rather than beside any one record because the writers span layers
#: that cannot import each other -- ``observability`` may depend on ``utils``
#: alone, and a second literal elsewhere would let one kind be raised on its
#: own, at which point the device that wrote it rejects its own file and its
#: queue stops.
SHARED_RECORD_SCHEMA_VERSION = 1

ChangeOperation = Literal["create", "update", "delete"]


@dataclass(frozen=True)
class ChangeSet:
    """One completed shared-state write, described for the sync queue.

    Attributes:
        change_id (str): Identifier unique within this device, used as the
            argument to :meth:`WorkspaceSyncPort.await_pushed`.
        operation (ChangeOperation): What happened to the paths.
        paths (tuple[str, ...]): Paths relative to ``.guildbotics/``.
    """

    change_id: str
    operation: ChangeOperation
    paths: tuple[str, ...]


class WorkspaceSyncPort(Protocol):
    """The contract storage layers use to announce and await shared writes."""

    def shared_state_changed(self, change: ChangeSet) -> bool:
        """Queue ``change`` for synchronization, returning whether it was accepted."""

    def await_pushed(self, change_id: str) -> bool:
        """Block until a commit containing ``change_id`` reaches the hub.

        Returns:
            bool: True only when the change is durably shared. Rejection by a
                concurrent update, invalid shared state, and an unreachable
                hub all return False.
        """


class NoOpWorkspaceSyncPort:
    """The port used while a workspace has synchronization disabled."""

    def shared_state_changed(self, change: ChangeSet) -> bool:
        return False

    def await_pushed(self, change_id: str) -> bool:
        return False


_port: WorkspaceSyncPort = NoOpWorkspaceSyncPort()


def get_workspace_sync_port() -> WorkspaceSyncPort:
    """Return the port that receives shared-state notifications."""
    return _port


def set_workspace_sync_port(port: WorkspaceSyncPort | None) -> None:
    """Install ``port``, or restore the no-op port when ``port`` is None."""
    global _port
    _port = port if port is not None else NoOpWorkspaceSyncPort()


def shared_relative_path(path: Path, workspace_root: Path | None = None) -> str | None:
    """Return the ``.guildbotics``-relative path when ``path`` is shared.

    Args:
        path (Path): An absolute or workspace-relative filesystem path.
        workspace_root (Path | None): The workspace, or None to use the selected one.

    Returns:
        str | None: A POSIX path such as ``state/devices/<id>.json``, or None
            when the path is outside the shared roots, is device-local
            (``local/``), or no workspace is selected.
    """
    try:
        root = get_workspace_root(workspace_root) / ".guildbotics"
    except WorkspaceNotConfiguredError:
        return None
    try:
        relative = path.expanduser().resolve(strict=False).relative_to(root.resolve())
    except ValueError:
        return None
    if not relative.parts or relative.parts[0] not in SHARED_ROOTS:
        return None
    return relative.as_posix()


def notify_shared_state_changed(
    operation: ChangeOperation,
    paths: list[Path],
    workspace_root: Path | None = None,
) -> ChangeSet | None:
    """Announce a completed write of ``paths`` to the sync port.

    Paths outside the shared roots are dropped, so callers may pass a mixed
    list without classifying it themselves.

    Returns:
        ChangeSet | None: The announced change, or None when nothing shared changed.
    """
    shared = tuple(
        relative
        for relative in (
            shared_relative_path(path, workspace_root=workspace_root) for path in paths
        )
        if relative is not None
    )
    if not shared:
        return None
    change = ChangeSet(change_id=uuid4().hex, operation=operation, paths=shared)
    get_workspace_sync_port().shared_state_changed(change)
    return change


def dump_shared_json(payload: Any) -> str:
    """Serialize ``payload`` the one way every device writes shared JSON.

    Stable key ordering and a trailing newline keep byte-identical content
    from producing spurious concurrent updates between devices.
    """
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_shared_bytes(
    path: Path, data: bytes, workspace_root: Path | None = None
) -> ChangeSet | None:
    """Atomically write ``data`` to ``path`` and announce the change."""
    operation: ChangeOperation = "update" if path.exists() else "create"
    atomic_write_bytes(path, data)
    return notify_shared_state_changed(operation, [path], workspace_root=workspace_root)


def write_shared_text(
    path: Path, text: str, workspace_root: Path | None = None
) -> ChangeSet | None:
    """Atomically write UTF-8 ``text`` to ``path`` and announce the change."""
    return write_shared_bytes(path, text.encode("utf-8"), workspace_root=workspace_root)


def write_shared_json(
    path: Path, payload: Any, workspace_root: Path | None = None
) -> ChangeSet | None:
    """Atomically write ``payload`` as stable JSON and announce the change."""
    return write_shared_text(
        path, dump_shared_json(payload), workspace_root=workspace_root
    )


def delete_shared_path(
    path: Path, workspace_root: Path | None = None
) -> ChangeSet | None:
    """Delete ``path`` when it exists and announce the change."""
    if not path.exists():
        return None
    path.unlink()
    return notify_shared_state_changed("delete", [path], workspace_root=workspace_root)
