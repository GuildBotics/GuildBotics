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
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from guildbotics.utils.advisory_lock import held_lock
from guildbotics.utils.fileio import get_workspace_local_path

#: How long each side waits for the other. Generous on purpose: a first copy
#: from a hub restores thousands of files inside this lock, and a save refused
#: there would be reported as an error the user can do nothing about.
LOCK_TIMEOUT_SECONDS = 30.0


def shared_write_lock_path(workspace_root: Path | None = None) -> Path:
    """Return the lock file both writers of a workspace's shared files take."""
    return get_workspace_local_path(
        "run", "shared-write.lock", workspace_root=workspace_root
    )


@contextmanager
def shared_write_lock(
    workspace_root: Path | None = None, timeout: float = LOCK_TIMEOUT_SECONDS
) -> Iterator[IO[str]]:
    """Hold a workspace's shared-write lock for the duration of the block.

    Args:
        workspace_root (Path | None): The workspace, or None to use the
            selected one.
        timeout (float): Seconds to wait for the other side before giving up.

    Yields:
        IO[str]: The open lock file handle.

    Raises:
        LockTimeoutError: When the other side holds it past ``timeout``.
    """
    with held_lock(shared_write_lock_path(workspace_root), timeout=timeout) as handle:
        yield handle
