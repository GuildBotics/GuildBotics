from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO
from uuid import uuid4

from fastapi import UploadFile

from guildbotics.utils.advisory_lock import (
    lock_file_nonblocking,
    open_lock_file,
    unlock_file,
)

COMMAND_INPUT_DIRECTORY_NAME = "guildbotics-command-inputs"
MAX_COMMAND_INPUT_FILE_BYTES = 20 * 1024 * 1024

_CLEANUP_LOCK_NAME = ".cleanup.lock"
_LOCK_RETRY_SECONDS = 0.01
_SESSION_DIRECTORY_PREFIX = "session-"
_SESSION_LOCK_NAME = ".session.lock"

_IMAGE_SUFFIXES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class CommandInputFileStore:
    """Own pasted command-input images for one App API session."""

    def __init__(self, *, temporary_root: Path | None = None) -> None:
        root = temporary_root or Path(tempfile.gettempdir())
        self._root = root / COMMAND_INPUT_DIRECTORY_NAME
        self._directory: Path | None = None
        self._session_lock: IO[str] | None = None

    def start(self) -> None:
        """Create this session directory after recovering orphaned sessions."""
        if self._directory is not None:
            return
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._root.chmod(0o700)
        with _cleanup_lock(self._root):
            _remove_orphaned_sessions(self._root)
            directory = Path(
                tempfile.mkdtemp(prefix=_SESSION_DIRECTORY_PREFIX, dir=self._root)
            )
            session_lock = open_lock_file(directory / _SESSION_LOCK_NAME)
            try:
                lock_file_nonblocking(session_lock)
            except Exception:
                session_lock.close()
                shutil.rmtree(directory, ignore_errors=True)
                raise
            self._directory = directory
            self._session_lock = session_lock

    def save(self, upload_file: UploadFile) -> Path:
        """Save an image in the active App API session directory."""
        if self._directory is None:
            raise RuntimeError("Command input file store is not running.")
        return save_command_input_file(self._directory, upload_file)

    def close(self) -> None:
        """Remove every image owned by this App API session."""
        directory = self._directory
        session_lock = self._session_lock
        self._directory = None
        self._session_lock = None
        if session_lock is not None:
            try:
                unlock_file(session_lock)
            finally:
                session_lock.close()
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)


def save_command_input_file(directory: Path, upload_file: UploadFile) -> Path:
    """Persist a pasted image and return its absolute path.

    Args:
        directory: App API session directory for pasted command inputs.
        upload_file: Image received from the Desktop clipboard.

    Returns:
        Absolute path to the persisted temporary input file.

    Raises:
        ValueError: If the upload is not a supported image or exceeds the limit.
    """
    content_type = (upload_file.content_type or "").lower()
    suffix = _IMAGE_SUFFIXES.get(content_type)
    if suffix is None:
        raise ValueError("Only PNG, JPEG, GIF, and WebP images are supported.")

    directory.chmod(0o700)
    destination = directory / f"{uuid4().hex}{suffix}"
    temporary = directory / f".{destination.name}.upload"
    size = 0
    try:
        with temporary.open("xb") as output:
            while chunk := upload_file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_COMMAND_INPUT_FILE_BYTES:
                    raise ValueError(
                        "Pasted image is too large "
                        f"(max {MAX_COMMAND_INPUT_FILE_BYTES // (1024 * 1024)} MB)."
                    )
                output.write(chunk)
        if size == 0:
            raise ValueError("Pasted image is empty.")
        os.replace(temporary, destination)
        destination.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return destination.resolve()


@contextmanager
def _cleanup_lock(root: Path) -> Iterator[None]:
    lock_file = open_lock_file(root / _CLEANUP_LOCK_NAME)
    while True:
        try:
            lock_file_nonblocking(lock_file)
            break
        except BlockingIOError:
            time.sleep(_LOCK_RETRY_SECONDS)
    try:
        yield
    finally:
        unlock_file(lock_file)
        lock_file.close()


def _remove_orphaned_sessions(root: Path) -> None:
    for directory in root.glob(f"{_SESSION_DIRECTORY_PREFIX}*"):
        if directory.is_symlink() or not directory.is_dir():
            continue
        session_lock = open_lock_file(directory / _SESSION_LOCK_NAME)
        try:
            try:
                lock_file_nonblocking(session_lock)
            except BlockingIOError:
                continue
            unlock_file(session_lock)
        finally:
            session_lock.close()
        shutil.rmtree(directory, ignore_errors=True)
