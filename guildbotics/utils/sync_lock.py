"""Process-wide serialization for operations on a workspace sync repository."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from guildbotics.utils.advisory_lock import workspace_reentrant_lock
from guildbotics.utils.fileio import get_workspace_local_path

# A sync cycle may include a network round trip. Keep the wait bounded so a
# member command can report that another process owns the repository instead
# of hanging behind an unresponsive queue forever.
LOCK_TIMEOUT_SECONDS = 30.0


class SyncRepositoryBusyError(RuntimeError):
    """Raised when another process owns a workspace's sync repository lock."""


def sync_lock_path(workspace_root: Path | None = None) -> Path:
    """Return the machine-wide lock path for a workspace sync repository."""
    return get_workspace_local_path("run", "sync.lock", workspace_root=workspace_root)


@contextmanager
def sync_repository_lock(
    workspace_root: Path | None = None,
    timeout: float | None = None,
) -> Iterator[None]:
    """Hold ``local/run/sync.lock`` for a complete repository operation.

    The lock is intentionally process-wide and includes network operations.
    Shared-file writers take their own lock inside this one; that ordering is
    what prevents a member one-shot from interleaving with checkout/converge.
    With no selected workspace there is no synchronization repository to
    serialize, so the context is a no-op.
    """
    wait = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    with workspace_reentrant_lock(
        workspace_root,
        path_parts=("run", "sync.lock"),
        key_prefix="sync",
        timeout=wait,
        timeout_error=lambda locked_path, locked_timeout: SyncRepositoryBusyError(
            f"Another sync operation held {locked_path} for longer than {locked_timeout:g}s."
        ),
    ):
        yield
