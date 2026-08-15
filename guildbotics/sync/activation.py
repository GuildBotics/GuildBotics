"""Putting the sync queue into a running process, and taking it out again.

This is the one place synchronization is wired to the rest of GuildBotics.
Everything else announces writes through the Workspace Sync Port and never
learns whether anything is listening, so a workspace with no hub keeps the
no-op port and issues no Git command and no SSH connection at all.

A process calls :func:`activate_workspace_sync` once it knows which workspace
it is working in, and again after that changes -- a workspace switch, or a
workspace that has just been connected to a hub. Activation is therefore
idempotent and safe to call when nothing is enabled.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from guildbotics.sync.local_repository import LocalSyncRepository, SyncRepositoryError
from guildbotics.sync.manager import GitSyncManager, build_git_sync_manager
from guildbotics.utils.fileio import WorkspaceNotConfiguredError
from guildbotics.utils.workspace_sync_port import set_workspace_sync_port

LOGGER = logging.getLogger(__name__)

_lock = threading.Lock()
_manager: GitSyncManager | None = None
_workspace: Path | None = None


def current_sync_manager() -> GitSyncManager | None:
    """Return the queue running in this process, or None when none is."""
    return _manager


def activate_workspace_sync(
    workspace_root: Path | None = None,
) -> GitSyncManager | None:
    """Run the sync queue for a workspace, if that workspace has a hub.

    Args:
        workspace_root (Path | None): The workspace, or None to use the selected one.

    Returns:
        GitSyncManager | None: The running queue, or None when this workspace
            is not connected to a hub and nothing was started.
    """
    global _manager, _workspace
    with _lock:
        repository = _connected_repository(workspace_root)
        if repository is None:
            _stop_locked()
            return None
        if _manager is not None and _workspace == repository.workspace_root:
            return _manager
        _stop_locked()
        manager = build_git_sync_manager(repository.workspace_root)
        set_workspace_sync_port(manager)
        manager.start()
        _manager = manager
        _workspace = repository.workspace_root
        return manager


def deactivate_workspace_sync() -> None:
    """Stop the queue and restore the no-op port.

    Called before a workspace switch so the queue of the workspace being left
    cannot commit into the one being entered.
    """
    global _manager, _workspace
    with _lock:
        _stop_locked()


def _stop_locked() -> None:
    """Stop whatever is running. The caller holds the activation lock."""
    global _manager, _workspace
    set_workspace_sync_port(None)
    if _manager is None:
        return
    if not _manager.stop():
        # A fetch or push can outlast the stop. The port is already detached,
        # so the thread finishes its cycle against the workspace it started in
        # and then exits; what must not happen is a second queue beside it,
        # which the manager's own start refuses while this one is alive.
        LOGGER.warning("The synchronization queue is still finishing its last cycle.")
    _manager = None
    _workspace = None


def _connected_repository(workspace_root: Path | None) -> LocalSyncRepository | None:
    """Return the workspace's repository when it has a hub, else None.

    Nothing here creates anything: a workspace only becomes a repository when
    the user enables synchronization on it.
    """
    try:
        repository = LocalSyncRepository(workspace_root)
        if not repository.initialized:
            return None
        repository.verify_boundary()
        return repository if repository.has_remote() else None
    except (WorkspaceNotConfiguredError, SyncRepositoryError, OSError):
        return None
