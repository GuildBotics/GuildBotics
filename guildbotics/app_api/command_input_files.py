from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path, PureWindowsPath
from typing import IO
from uuid import uuid4

from fastapi import UploadFile

from guildbotics.app_api.errors import AppApiError
from guildbotics.environment.contract import AccessContractError
from guildbotics.utils.advisory_lock import (
    lock_file_nonblocking,
    unlock_file,
)
from guildbotics.utils.fileio import get_machine_state_path
from guildbotics.utils.safe_paths import (
    inspect_host_path,
    normalize_host_path,
    open_host_file,
    visit_host_directory,
)

#: Largest file the Desktop hands a command.
MAX_COMMAND_INPUT_FILE_BYTES = 20 * 1024 * 1024

#: The longest name an upload keeps: file systems limit a name to 255 bytes,
#: and a character takes up to four of them.
_NAME_CHARS = 60
_CLEANUP_LOCK_NAME = ".cleanup.lock"
_LOCK_RETRY_SECONDS = 0.01
_SESSION_DIRECTORY_PREFIX = "session-"
_SESSION_LOCK_NAME = ".session.lock"


class CommandInputFileStore:
    """Own what the Desktop hands a command for one App API session.

    Everything lives under the machine-local state root, which no microVM
    ever mounts but this session's own directory, read-only, into the
    commands the session runs (:attr:`directory`), at the same path: a path
    pasted into the input field means the same file inside the environment,
    and no command can change what the host saved there. It is all temporary
    by nature -- a clipboard image exists nowhere else, an uploaded file has
    its original elsewhere -- and goes with the session that made it.
    """

    def __init__(self, *, root: Path | None = None) -> None:
        self._root = root or get_machine_state_path("command_inputs")
        self._directory: Path | None = None
        self._session_lock: IO[str] | None = None
        self._lock = threading.RLock()
        self.problem = ""

    def start(self) -> None:
        """Create this session directory after recovering orphaned sessions."""
        with self._lock:
            try:
                self._start()
            except AccessContractError as exc:
                self.problem = str(exc)
                raise
            self.problem = ""

    def _start(self) -> None:
        if self._directory is not None:
            return
        self._root = inspect_host_path(self._root, create=True).path
        visit_host_directory(self._root, self._start_in_directory)

    def _start_in_directory(self, descriptor: int | None) -> None:
        """Create a session while the checked exchange ancestry remains open."""
        with _cleanup_lock(self._root):
            _remove_orphaned_sessions(self._root, descriptor)
            name = f"{_SESSION_DIRECTORY_PREFIX}{uuid4().hex}"
            if descriptor is None:
                inspect_host_path(self._root / name, create=True)
            else:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
            directory = self._root / name
            session_lock = open_host_file(directory / _SESSION_LOCK_NAME)
            try:
                lock_file_nonblocking(session_lock)
            except Exception:
                session_lock.close()
                shutil.rmtree(directory, ignore_errors=True)
                raise
            self._directory = directory
            self._session_lock = session_lock

    @property
    def directory(self) -> Path | None:
        """This session's directory, which the commands it runs read; none
        until the session started."""
        return self._directory

    def save(self, upload_file: UploadFile) -> Path:
        """Save an uploaded file in the active App API session directory."""
        return save_command_input_file(self._session_directory(), upload_file)

    def close(self) -> None:
        """Remove everything owned by this App API session."""
        with self._lock:
            self._close()

    def _close(self) -> None:
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
            with suppress(AccessContractError, OSError):
                visit_host_directory(
                    directory.parent, lambda fd: _remove_session(directory, fd)
                )

    def _session_directory(self) -> Path:
        if self._directory is None:
            self.start()
        assert self._directory is not None
        return self._directory


def save_command_input_file(directory: Path, upload_file: UploadFile) -> Path:
    """Persist an uploaded file -- a pasted image, or a file a browser dropped,
    which cannot hand over a path -- and return its absolute path.

    The file keeps its name behind a short random prefix: the name is what
    tells an agent what the file is, and the prefix is what keeps two uploads
    of the same name apart.

    Args:
        directory: App API session directory for command inputs.
        upload_file: The file the screen sent.

    Returns:
        Absolute path to the persisted file.

    Raises:
        ValueError: If the upload is empty or exceeds the limit.
    """
    # Either separator: the name is the screen's, of a file on any machine.
    # Its end is what says what the file is; the start goes when it is long.
    name = PureWindowsPath(upload_file.filename or "").name.strip()[-_NAME_CHARS:]
    name = f"{uuid4().hex[:8]}-{name or 'file'}"
    directory = normalize_host_path(directory)
    destination = directory / name
    temporary = directory / f".{name}.upload"

    def save(fd: int | None) -> None:
        try:
            # Windows holds every ancestor without delete sharing; on POSIX
            # the descriptor anchors the complete write/rename/cleanup.
            handle = os.open(
                temporary if fd is None else temporary.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=fd,
            )
            size = 0
            with os.fdopen(handle, "wb") as output:
                while chunk := upload_file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_COMMAND_INPUT_FILE_BYTES:
                        raise ValueError(
                            "File is too large (max "
                            f"{MAX_COMMAND_INPUT_FILE_BYTES // (1024 * 1024)} MB)."
                        )
                    output.write(chunk)
            if size == 0:
                raise ValueError("File is empty.")
            os.replace(
                temporary if fd is None else temporary.name,
                destination if fd is None else destination.name,
                src_dir_fd=fd,
                dst_dir_fd=fd,
            )
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary if fd is None else temporary.name, dir_fd=fd)

    visit_host_directory(directory, save)
    return destination


def _remove_session(directory: Path, fd: int | None) -> None:
    shutil.rmtree(
        directory if fd is None else directory.name, dir_fd=fd, ignore_errors=True
    )


def command_cwd(cwd: Path | None) -> Path | None:
    """The working directory as the Desktop typed it, ready to run in.

    ``~`` is expanded, as a shell would, because the field has no shell in
    front of it. A relative path is refused: the server's own directory is
    nowhere the user can see, so there is nothing to resolve it against.
    """
    if cwd is None:
        return None
    expanded = cwd.expanduser()
    if not expanded.is_absolute():
        raise AppApiError("command_cwd_not_absolute", params={"cwd": str(cwd)})
    return normalize_host_path(expanded)


@contextmanager
def _cleanup_lock(root: Path) -> Iterator[None]:
    lock_file = open_host_file(root / _CLEANUP_LOCK_NAME)
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


def _remove_orphaned_sessions(root: Path, descriptor: int | None) -> None:
    for name in os.listdir(root if descriptor is None else descriptor):
        if not name.startswith(_SESSION_DIRECTORY_PREFIX):
            continue
        directory = root / name
        try:
            inspect_host_path(directory)
            session_lock = open_host_file(directory / _SESSION_LOCK_NAME)
        except AccessContractError:
            continue
        try:
            try:
                lock_file_nonblocking(session_lock)
            except BlockingIOError:
                continue
            unlock_file(session_lock)
        finally:
            session_lock.close()
        if descriptor is None:
            shutil.rmtree(directory, ignore_errors=True)
        else:
            shutil.rmtree(name, dir_fd=descriptor, ignore_errors=True)
