"""Putting the sync queue into a running process, and taking it out again.

This is the one place synchronization is wired to the rest of GuildBotics, and
the one place that decides which process-wide queue is running. Everything that
starts one, stops one, or needs the repository to itself goes through the same
lock here, so those never overlap.
Everything else announces writes through the Workspace Sync Port and never
learns whether anything is listening, so a workspace with no hub keeps the
no-op port and issues no Git command and no SSH connection at all.

A process calls :func:`activate_workspace_sync` once it knows which workspace
it is working in, and again after that changes -- a workspace switch, or a
workspace that has just been connected to a hub. Activation is therefore
idempotent and safe to call when nothing is enabled.

The process-wide repository lock below also covers the member one-shot and
``guildbotics start``. The queue remains one per process, but no second
process can operate the same repository at the same time.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from guildbotics.sync.local_repository import LocalSyncRepository, SyncRepositoryError
from guildbotics.sync.manager import (
    GitSyncManager,
    GitSyncStatus,
    build_git_sync_manager,
)
from guildbotics.utils.fileio import WorkspaceNotConfiguredError
from guildbotics.utils.sync_lock import sync_repository_lock
from guildbotics.utils.workspace_sync_port import set_workspace_sync_port

LOGGER = logging.getLogger(__name__)

_lock = threading.Lock()
_manager: GitSyncManager | None = None
_workspace: Path | None = None


def current_sync_manager() -> GitSyncManager | None:
    """Return the queue running in this process, or None when none is."""
    return _manager


ONE_SHOT_LOCK_TIMEOUT_SECONDS = 1.0


def commit_and_push_once(
    workspace_root: Path | None = None,
    *,
    timeout: float | None = None,
) -> GitSyncStatus | None:
    """Commit and push one member-CLI write when synchronization is enabled.

    A running queue is reused so its pending barriers and diagnostics remain
    attached to the same manager. When the workspace is connected but this
    process has no queue (the normal member-CLI case), a short-lived manager
    performs exactly one locked commit/push and is then discarded.
    """
    root = LocalSyncRepository(workspace_root).workspace_root
    with _lock:
        manager = _manager if _workspace == root else None
        if manager is None:
            repository = LocalSyncRepository(root)
            if not repository.initialized or not repository.has_remote():
                return None
            manager = build_git_sync_manager(root)
        # Keep the lifecycle lock through the manager call. Otherwise a
        # concurrent workspace switch can stop or replace the manager after
        # it was selected, leaving this one-shot operation with a stale queue
        # object and an uncoordinated repository access.
        return manager.commit_and_push_once(timeout=timeout)


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
    with _lock:
        return _activate_locked(workspace_root)


@contextmanager
def paused_workspace_sync(workspace_root: Path | None = None) -> Iterator[None]:
    """Hold one workspace's repository alone, with no queue running in it.

    Enrolling and previewing commit, fetch, reset the branch, and move refs in
    the very repository the queue works in, so the queue stops for the duration.
    The lock is held across the whole body rather than only across the stop:
    releasing it in between would let a second request see no manager and walk
    into the same repository, and would let an activation start a queue beside
    the work in progress.

    Raises:
        SyncStillStoppingError: When the queue could not be stopped. Whatever
            was going to be done here would have been done beside it.
    """
    with _lock:
        lock_root = workspace_root or _workspace
        if not _stop_locked():
            raise SyncStillStoppingError(
                "The synchronization queue has not finished stopping. "
                "Try again in a moment."
            )
        with sync_repository_lock(lock_root):
            try:
                yield
            finally:
                # Restored whether or not the work succeeded: a failed attempt
                # must not leave a workspace that has a hub quietly not
                # synchronizing. Activation happens before the lock is
                # released, so another process cannot enter the repository
                # between the operation and the queue's restart.
                _activate_locked(workspace_root)


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


def _activate_locked(workspace_root: Path | None) -> GitSyncManager | None:
    """Start the queue for a workspace. The caller holds the activation lock."""
    global _manager, _workspace
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
