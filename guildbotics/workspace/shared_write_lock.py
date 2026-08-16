"""The one lock that serializes writes to a workspace's shared files.

Two things write those files, and neither knows about the other. A config save
compares the revisions it was composed against and then writes several files;
the synchronization queue checks the hub's content out over the same files and
commits whatever the working tree holds. Each is careful on its own terms, and
that is not enough on its own: a save whose comparison has just passed can land
on top of content the queue adopted while the save was running, and because
that overwrite is an ordinary local write, the next cycle commits and pushes
it. Nothing in the sync history records the other device's change as lost --
which is exactly the outcome the comparison exists to prevent.

So both hold this lock, and hold it across the whole of what they do to the
files: comparing and writing on one side, checking out and committing on the
other. Nothing holds it across the network, so a save never waits on a hub.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import IO

from guildbotics.utils.advisory_lock import LockTimeoutError, held_lock
from guildbotics.utils.fileio import (
    WorkspaceNotConfiguredError,
    get_workspace_local_path,
)

#: How long each side waits for the other. Generous on purpose: a first copy
#: from a hub restores thousands of files inside this lock, and a save refused
#: there would be reported as an error the user can do nothing about.
LOCK_TIMEOUT_SECONDS = 30.0


class SharedWriteBusyError(RuntimeError):
    """Raised when the other writer held the lock for the whole wait.

    Deliberately outside the ``OSError`` family that :class:`TimeoutError`
    belongs to. Synchronization catches that family to mean "the environment
    failed" and reports the hub unreachable; the hub has nothing to do with
    this, and a caller that has to tell the two apart should not have to name
    a lock timeout to do it.
    """


def shared_write_lock_path(workspace_root: Path | None = None) -> Path:
    """Return the lock file both writers of a workspace's shared files take."""
    return get_workspace_local_path(
        "run", "shared-write.lock", workspace_root=workspace_root
    )


@contextmanager
def shared_write_lock(
    workspace_root: Path | None = None, timeout: float = LOCK_TIMEOUT_SECONDS
) -> Iterator[IO[str] | None]:
    """Hold a workspace's shared-write lock for the duration of the block.

    With no workspace selected there is nothing to serialize: no file is
    shared, and no queue exists to adopt a hub's content over one. Writers
    that resolve the workspace the way the sync port does reach this state
    normally, so the exception is answered here rather than by each of them.

    Args:
        workspace_root (Path | None): The workspace, or None to use the
            selected one.
        timeout (float): Seconds to wait for the other side before giving up.

    Yields:
        IO[str] | None: The open lock file handle, or None when no workspace
            is selected.

    Raises:
        SharedWriteBusyError: When the other side holds it past ``timeout``.
    """
    try:
        path = shared_write_lock_path(workspace_root)
    except WorkspaceNotConfiguredError:
        yield None
        return
    stack = ExitStack()
    # Only the acquisition is translated. A timeout raised by the body belongs
    # to whatever the body was doing, not to waiting for this lock.
    try:
        handle = stack.enter_context(held_lock(path, timeout=timeout))
    except LockTimeoutError as exc:
        raise SharedWriteBusyError(
            f"Another writer held {path} for longer than {timeout:g}s."
        ) from exc
    with stack:
        yield handle
