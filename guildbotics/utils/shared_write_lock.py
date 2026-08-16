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
is exactly the outcome the comparison exists to prevent. A write that lands
between the queue's validation and its ``git add`` is worse still: content
nothing checked becomes shared history, and the devices that receive it stop
their queues.

So every writer holds this lock, and holds it across the whole of what it does
to the files -- from the read a write is derived from to the last file written.
Nothing holds it across the network, so a save never waits on a hub.

Which writers those are is not a judgement anyone makes per writer. Every write
to a shared path goes through :mod:`guildbotics.utils.workspace_sync_port`, and
those helpers call :func:`require_shared_write_lock`, so a writer that has not
declared its span fails immediately and by name. The lock is deliberately not
taken by the helpers themselves: a lock acquired per write would leave the read
a read-modify-write derives from outside the span, which looks protected and is
not. Declaring the span is the writer's job precisely because only the writer
knows where its read began.

Within one thread the lock re-enters, so a writer declares its own span without
having to know whether a caller already declared a wider one. That question --
"does my caller hold it?" -- is what was being answered per writer, wrongly and
repeatedly: a config save holds the lock across the several files it compares
and writes, and the writers it calls are the same ones a test, a CLI, or a
future caller reaches directly. An outer span simply subsumes the inner ones.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from functools import wraps
from pathlib import Path
from typing import IO

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


class SharedWriteLockRequiredError(RuntimeError):
    """Raised when a shared file is written outside the lock.

    A programming error rather than a condition to handle: the caller has to
    decide what its span is -- where its read begins and its last write ends --
    and no default the write helper could pick would be right for it.
    """

    def __init__(self, paths: Sequence[str]) -> None:
        listed = ", ".join(paths)
        super().__init__(
            f"{listed} is shared, so the change must be made inside "
            "shared_write_lock(). Hold it from the read the write derives "
            "from to the last file written."
        )
        self.paths = tuple(paths)


def _held_roots() -> dict[Path, tuple[int, IO[str] | None]]:
    roots: dict[Path, tuple[int, IO[str] | None]] | None = getattr(_held, "roots", None)
    if roots is None:
        roots = {}
        _held.roots = roots
    return roots


def shared_write_lock_path(workspace_root: Path | None = None) -> Path:
    """Return the lock file every writer of a workspace's shared files takes."""
    return get_workspace_local_path(
        "run", "shared-write.lock", workspace_root=workspace_root
    )


def shared_write_lock_held(workspace_root: Path | None = None) -> bool:
    """Return whether this thread holds ``workspace_root``'s shared-write lock."""
    try:
        root = get_workspace_root(workspace_root)
    except WorkspaceNotConfiguredError:
        return False
    return root in _held_roots()


def require_shared_write_lock(
    paths: Sequence[str], workspace_root: Path | None = None
) -> None:
    """Refuse a change to shared ``paths`` made outside the lock.

    Args:
        paths (Sequence[str]): The ``.guildbotics``-relative paths being
            changed, for the message.
        workspace_root (Path | None): The workspace, or None for the selected one.

    Raises:
        SharedWriteLockRequiredError: When this thread does not hold the lock.
    """
    if not paths or shared_write_lock_held(workspace_root):
        return
    raise SharedWriteLockRequiredError(paths)


@contextmanager
def shared_write_lock(
    workspace_root: Path | None = None, timeout: float | None = None
) -> Iterator[IO[str] | None]:
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

    Yields:
        IO[str] | None: The open lock file handle, or None when no workspace
            is selected.

    Raises:
        SharedWriteBusyError: When the other side holds it past ``timeout``.
    """
    wait = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        path = shared_write_lock_path(workspace_root)
        root = get_workspace_root(workspace_root)
    except WorkspaceNotConfiguredError:
        yield None
        return
    held = _held_roots()
    entered = held.get(root)
    if entered is not None:
        # Already this thread's: a wider span is in progress and subsumes this
        # one. Taking the file lock again would block on this thread's own
        # handle until the timeout and then blame a writer that is not there.
        depth, handle = entered
        held[root] = (depth + 1, handle)
        try:
            yield handle
        finally:
            depth, handle = held[root]
            held[root] = (depth - 1, handle)
        return
    stack = ExitStack()
    # Only the acquisition is translated. A timeout raised by the body belongs
    # to whatever the body was doing, not to waiting for this lock.
    try:
        handle = stack.enter_context(held_lock(path, timeout=wait))
    except LockTimeoutError as exc:
        raise SharedWriteBusyError(
            f"Another writer held {path} for longer than {wait:g}s."
        ) from exc
    held[root] = (1, handle)
    try:
        with stack:
            yield handle
    finally:
        del held[root]


def shared_write_operation[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Declare that the whole of ``func`` is one change to shared files.

    For an operation whose span is simply itself -- it reads what it needs and
    writes what it decided, and nothing in between waits on anything remote.
    Where the span is narrower than the function, or the workspace is not the
    selected one, take :func:`shared_write_lock` directly instead.
    """

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with shared_write_lock():
            return func(*args, **kwargs)

    return wrapper
