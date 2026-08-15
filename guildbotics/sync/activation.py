"""Putting the sync queue into a running process, and taking it out again.

This is the one place synchronization is wired to the rest of GuildBotics.
Everything else announces writes through the Workspace Sync Port and never
learns whether anything is listening, so a workspace with no hub keeps the
no-op port and issues no Git command and no SSH connection at all.

A process calls :func:`activate_workspace_sync` once it knows which workspace
it is working in, and again after that changes -- a workspace switch, or a
workspace that has just been connected to a hub. Activation is therefore
idempotent and safe to call when nothing is enabled.

**One process per machine may do so.** The guards here are module state, so
they hold within a process and not between two: a second process activating the
same workspace would put another thread on the same repository, interleaving its
resets, checkouts, and commits with the first. Nothing detects that, and the
machine-wide service lock does not cover it -- the desktop backend takes that
lock only when its scheduler starts, while the queue runs for as long as the
backend is up. Until a machine-wide owner exists (the device agent of §14.4),
the desktop backend is that one process.
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


class SyncStillStoppingError(RuntimeError):
    """Raised when the previous workspace's queue has not finished stopping."""


def activate_workspace_sync(
    workspace_root: Path | None = None,
) -> GitSyncManager | None:
    """Run the sync queue for a workspace, if that workspace has a hub.

    Args:
        workspace_root (Path | None): The workspace, or None to use the selected one.

    Returns:
        GitSyncManager | None: The running queue, or None when this workspace
            is not connected to a hub and nothing was started.

    Raises:
        SyncStillStoppingError: When a previous queue is still finishing. It is
            holding a repository, so starting another one now would put two
            threads on it.
    """
    global _manager, _workspace
    with _lock:
        repository = _connected_repository(workspace_root)
        if repository is None:
            _stop_locked()
            return None
        if _manager is not None and _workspace == repository.workspace_root:
            return _manager
        if not _stop_locked():
            raise SyncStillStoppingError(
                "The previous workspace's synchronization queue has not finished "
                "stopping. Try again in a moment."
            )
        manager = build_git_sync_manager(repository.workspace_root)
        set_workspace_sync_port(manager)
        manager.start()
        _manager = manager
        _workspace = repository.workspace_root
        return manager


def deactivate_workspace_sync() -> bool:
    """Stop the queue and restore the no-op port.

    Called before a workspace switch so the queue of the workspace being left
    cannot commit into the one being entered.

    Returns:
        bool: Whether the queue actually stopped. False means the switch it was
            making room for must not go ahead.
    """
    with _lock:
        return _stop_locked()


def _stop_locked() -> bool:
    """Stop whatever is running. The caller holds the activation lock.

    A worker that outlasts its stop keeps its repository: a fetch or a push can
    block far longer than the timeout, and the thread is still committing and
    resetting in there. Forgetting it would let the next activation build a
    second queue on the same repository -- two threads interleaving ``reset``,
    ``checkout``, and ``commit``, which manufactures rejections and anomalies
    out of nothing. So it stays, and the port stays attached to it, until it is
    really gone.
    """
    global _manager, _workspace
    if _manager is None:
        set_workspace_sync_port(None)
        return True
    if not _manager.stop():
        LOGGER.warning("The synchronization queue is still finishing its last cycle.")
        return False
    set_workspace_sync_port(None)
    _manager = None
    _workspace = None
    return True


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
