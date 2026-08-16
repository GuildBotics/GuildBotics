"""The one lock that serializes writes to a workspace's shared files.

Several things write those files, and none of them knows about the others. A
config save compares the revisions it was composed against and then writes
several files; a member's memory reads a document and writes it back; the
synchronization queue checks the hub's content out over the same files and
commits whatever the working tree holds. Each is careful on its own terms, and
that is not enough: a save whose comparison has just passed can land on top of
content the queue adopted while the save was running, and because that
overwrite is an ordinary local write, the next cycle commits and pushes it.
Nothing in the sync history records the other device's change as lost -- which
is exactly the outcome the comparison exists to prevent.

Writers do not take this lock one by one. Every write to a shared path goes
through :mod:`guildbotics.utils.workspace_sync_port`, and those helpers take
it, so a write that reads nothing is already serialized against the queue with
nothing to declare. What the helpers cannot infer is how far back a write's
span reaches: a value derived from a file's current contents has to exclude the
queue from the read as well, or the read is of content the checkout is about to
replace and the write puts it back. That is what
:func:`~guildbotics.utils.workspace_sync_port.update_shared_text` and its JSON
form are for -- they take the lock, read, and write inside it, so the span
starts at the read by construction. An operation whose span is wider still --
several files that have to land together, a decision made by scanning a
directory -- takes this lock itself, which is the one case left where a writer
has to know it is doing something the helpers cannot see.

Nothing holds it across the network, so a save never waits on a hub.

Within one thread the lock re-enters, so a writer declares its own span without
having to know whether a caller already declared a wider one. A config save
holds the lock across the several files it compares and writes, and the writers
it calls are the same ones a test, a CLI, or a future caller reaches directly.
An outer span simply subsumes the inner ones.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from functools import wraps
from pathlib import Path

from guildbotics.utils.advisory_lock import LockTimeoutError, held_lock
from guildbotics.utils.fileio import (
    WorkspaceNotConfiguredError,
    get_workspace_local_path,
    get_workspace_root,
)

#: How long each side waits for the other. Generous on purpose: a first copy
#: from a hub restores thousands of files inside this lock, and a save refused
#: there would be reported as an error the user can do nothing about.
LOCK_TIMEOUT_SECONDS = 30.0

#: Workspaces whose lock the current thread holds, and how deep. Ownership is
#: per thread because that is what the lock actually orders: a second thread of
#: the same process blocks on the file lock like any other process would.
_held = threading.local()


class SharedWriteBusyError(RuntimeError):
    """Raised when the other writer held the lock for the whole wait.

    Deliberately outside the ``OSError`` family that :class:`TimeoutError`
    belongs to. Synchronization catches that family to mean "the environment
    failed" and reports the hub unreachable; the hub has nothing to do with
    this, and a caller that has to tell the two apart should not have to name
    a lock timeout to do it.
    """


def _held_depths() -> dict[Path, int]:
    depths: dict[Path, int] | None = getattr(_held, "depths", None)
    if depths is None:
        depths = {}
        _held.depths = depths
    return depths


def shared_write_lock_path(workspace_root: Path | None = None) -> Path:
    """Return the lock file every writer of a workspace's shared files takes."""
    return get_workspace_local_path(
        "run", "shared-write.lock", workspace_root=workspace_root
    )


@contextmanager
def shared_write_lock(
    workspace_root: Path | None = None, timeout: float | None = None
) -> Iterator[None]:
    """Hold a workspace's shared-write lock for the duration of the block.

    With no workspace selected there is nothing to serialize: no file is
    shared, and no queue exists to adopt a hub's content over one. Writers
    that resolve the workspace the way the sync port does reach this state
    normally, so the exception is answered here rather than by each of them.

    Args:
        workspace_root (Path | None): The workspace, or None to use the
            selected one.
        timeout (float | None): Seconds to wait for the other side before
            giving up, or None for :data:`LOCK_TIMEOUT_SECONDS`. Resolved on
            the call rather than bound as a default, so the wait every writer
            inherits stays one value that can be answered in one place.

    Raises:
        SharedWriteBusyError: When the other side holds it past ``timeout``.
    """
    wait = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        path = shared_write_lock_path(workspace_root)
        root = get_workspace_root(workspace_root)
    except WorkspaceNotConfiguredError:
        yield
        return
    depths = _held_depths()
    if root in depths:
        # Already this thread's: a wider span is in progress and subsumes this
        # one. Taking the file lock again would block on this thread's own
        # handle until the timeout and then blame a writer that is not there.
        depths[root] += 1
        try:
            yield
        finally:
            depths[root] -= 1
        return
    stack = ExitStack()
    # Only the acquisition is translated. A timeout raised by the body belongs
    # to whatever the body was doing, not to waiting for this lock.
    try:
        stack.enter_context(held_lock(path, timeout=wait))
    except LockTimeoutError as exc:
        raise SharedWriteBusyError(
            f"Another writer held {path} for longer than {wait:g}s."
        ) from exc
    depths[root] = 1
    try:
        with stack:
            yield
    finally:
        del depths[root]


def shared_write_operation[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Declare that the whole of ``func`` is one change to shared files.

    For an operation whose span is the function itself: it decides what to
    write by reading shared files, or writes several that have to land
    together, and nothing in between waits on anything remote. Where the span
    is narrower than the function, or the workspace is not the selected one,
    take :func:`shared_write_lock` directly instead.
    """

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with shared_write_lock():
            return func(*args, **kwargs)

    return wrapper
