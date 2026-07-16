"""Small cross-platform advisory file-lock primitives."""

from __future__ import annotations

import errno
import os
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


def unlock_file(handle: IO[str]) -> None:
    if not _WINDOWS:
        _posix_locking.flock(handle.fileno(), _posix_locking.LOCK_UN)
        return
    handle.seek(0)
    _windows_locking.locking(handle.fileno(), _windows_locking.LK_UNLCK, 1)


def _ensure_lock_byte(handle: IO[str]) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(" ")
        handle.flush()
