"""Small cross-platform advisory file-lock primitives."""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

_WINDOWS = os.name == "nt"
_windows_locking: Any
_posix_locking: Any
if _WINDOWS:
    import msvcrt

    _windows_locking = msvcrt
    _posix_locking = None
else:
    import fcntl

    _windows_locking = None
    _posix_locking = fcntl


def open_lock_file(path: Path) -> IO[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    return os.fdopen(descriptor, "r+", encoding="utf-8")


def lock_file_nonblocking(handle: IO[str]) -> None:
    if not _WINDOWS:
        _posix_locking.flock(
            handle.fileno(), _posix_locking.LOCK_EX | _posix_locking.LOCK_NB
        )
        return
    _ensure_lock_byte(handle)
    handle.seek(0)
    try:
        _windows_locking.locking(handle.fileno(), _windows_locking.LK_NBLCK, 1)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise BlockingIOError(exc.errno, exc.strerror) from exc
        raise


class LockTimeoutError(TimeoutError):
    """Raised when an advisory lock stays held past the caller's deadline."""


@contextmanager
def held_lock(
    path: Path, timeout: float = 5.0, poll_interval: float = 0.01
) -> Iterator[IO[str]]:
    """Hold the advisory lock at ``path`` for the duration of the block.

    The lock guards a short critical section, not a user-visible edit session,
    so it carries no owner metadata and no TTL: the OS releases it when the
    holding process exits.

    Args:
        path (Path): The lock file. Parent directories are created.
        timeout (float): Seconds to keep retrying before giving up.
        poll_interval (float): Seconds between retries.

    Yields:
        IO[str]: The open lock file handle.

    Raises:
        LockTimeoutError: When the lock stays held for longer than ``timeout``.
    """
    handle = open_lock_file(path)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                lock_file_nonblocking(handle)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"Timed out waiting for the advisory lock at {path}."
                    ) from None
                time.sleep(poll_interval)
        try:
            yield handle
        finally:
            unlock_file(handle)
    finally:
        handle.close()


def unlock_file(handle: IO[str]) -> None:
    if not _WINDOWS:
        _posix_locking.flock(handle.fileno(), _posix_locking.LOCK_UN)
        return
    handle.seek(0)
    _windows_locking.locking(handle.fileno(), _windows_locking.LK_UNLCK, 1)


def read_lock_data(handle: IO[str]) -> str:
    """Read data stored after the byte reserved for Windows locking."""
    handle.seek(1)
    return handle.read()


def write_lock_data(handle: IO[str], value: str) -> None:
    """Replace data while preserving the leading byte used for locking."""
    handle.seek(0)
    handle.write(" ")
    handle.write(value)
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


def _ensure_lock_byte(handle: IO[str]) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(" ")
        handle.flush()
