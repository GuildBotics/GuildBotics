from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal
from uuid import uuid4

from fastapi import UploadFile

from guildbotics.intelligences.agent_environment.contract import (
    AccessContractError,
    exchange_tmp_dir,
    grant_spelling,
    load_local_grants,
    load_shared_grants,
    resolve_access,
)
from guildbotics.utils.advisory_lock import (
    lock_file_nonblocking,
    open_lock_file,
    unlock_file,
)

#: Largest file the Desktop hands a command; not a shared file, so not derived
#: from the sync boundary's limits.
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

InputPathKind = Literal["file", "directory", "missing"]
GrantScope = Literal["document", "device"]


class CommandInputFileStore:
    """Own what the Desktop hands a command for one App API session.

    Everything lives in the exchange directory's ``tmp`` (see
    :mod:`guildbotics.intelligences.agent_environment.contract`), which the
    default grant opens to every turn read-write, so a path pasted into the
    input field means the same file inside the environment. It is all
    temporary by nature -- a clipboard image exists nowhere else, a copy has
    its original elsewhere -- and goes with the session that made it.
    """

    def __init__(self, *, root: Path | None = None) -> None:
        self._root = root or exchange_tmp_dir()
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
        return save_command_input_file(self._session_directory(), upload_file)

    def copy(self, source: Path) -> Path:
        """Copy a file of the user's into the active session directory."""
        return copy_command_input_file(self._session_directory(), source)

    def close(self) -> None:
        """Remove everything owned by this App API session."""
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

    def _session_directory(self) -> Path:
        if self._directory is None:
            raise RuntimeError("Command input file store is not running.")
        return self._directory


def save_command_input_file(directory: Path, upload_file: UploadFile) -> Path:
    """Persist a pasted image and return its absolute path.

    Args:
        directory: App API session directory for command inputs.
        upload_file: Image received from the Desktop clipboard.

    Returns:
        Absolute path to the persisted file.

    Raises:
        ValueError: If the upload is not a supported image or exceeds the limit.
    """
    content_type = (upload_file.content_type or "").lower()
    suffix = _IMAGE_SUFFIXES.get(content_type)
    if suffix is None:
        raise ValueError("Only PNG, JPEG, GIF, and WebP images are supported.")

    def write(output: IO[bytes]) -> int:
        size = 0
        while chunk := upload_file.file.read(1024 * 1024):
            size += len(chunk)
            _check_size(size)
            output.write(chunk)
        return size

    return _write_command_input_file(directory, f"{uuid4().hex}{suffix}", write)


def copy_command_input_file(directory: Path, source: Path) -> Path:
    """Copy one of the user's files beside the pasted images and return the copy.

    The copy keeps the file's name behind a short random prefix: the name is
    what tells an agent what the file is, and the prefix is what keeps two
    drops of the same name apart.

    Args:
        directory: App API session directory for command inputs.
        source: The file as the Desktop names it.

    Raises:
        ValueError: If ``source`` is not a regular file or exceeds the limit.
    """
    if not source.is_file():
        raise ValueError(f"'{source}' is not a file.")
    _check_size(source.stat().st_size)

    def write(output: IO[bytes]) -> int:
        with source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
        return output.tell()

    return _write_command_input_file(
        directory, f"{uuid4().hex[:8]}-{source.name}", write
    )


def _check_size(size: int) -> None:
    if size > MAX_COMMAND_INPUT_FILE_BYTES:
        raise ValueError(
            f"File is too large (max {MAX_COMMAND_INPUT_FILE_BYTES // (1024 * 1024)} MB)."
        )


def _write_command_input_file(
    directory: Path, name: str, write: Callable[[IO[bytes]], int]
) -> Path:
    directory.chmod(0o700)
    destination = directory / name
    temporary = directory / f".{name}.upload"
    try:
        with temporary.open("xb") as output:
            size = write(output)
        if size == 0:
            raise ValueError("File is empty.")
        os.replace(temporary, destination)
        destination.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return destination.resolve()


@dataclass(frozen=True, slots=True)
class GrantSuggestion:
    """The grant that would open an unreachable path: its directory, spelled
    as the grants screen wants it -- a document grant under the home, a
    device path elsewhere."""

    scope: GrantScope
    path: str


@dataclass(frozen=True, slots=True)
class CommandInputPath:
    """One path the Desktop is about to put in the input field, as a turn
    on this device would find it."""

    path: Path
    kind: InputPathKind
    reachable: bool
    grant: GrantSuggestion | None = None


def describe_command_input_paths(
    paths: Iterable[Path], cwd: Path | None = None
) -> list[CommandInputPath]:
    """Whether a turn run in ``cwd`` would see each path, and if not, which
    grant would let it.

    Grants that cannot be resolved make every path unreachable, which is also
    what the turn would say: it refuses to start.
    """
    try:
        access = resolve_access(load_shared_grants(), load_local_grants(), create=False)
    except AccessContractError:
        access = None
    home = Path.home().resolve()
    described = []
    for path in paths:
        kind: InputPathKind = (
            "directory" if path.is_dir() else "file" if path.exists() else "missing"
        )
        reachable = access is not None and access.reaches(path, cwd)
        grant = None
        if not reachable and kind != "missing":
            real = path.resolve()
            grant = _grant_for(real if kind == "directory" else real.parent, home)
        described.append(CommandInputPath(path, kind, reachable, grant))
    return described


def _grant_for(directory: Path, home: Path) -> GrantSuggestion | None:
    """The home directory itself cannot be granted; anything else can."""
    if directory == home:
        return None
    scope: GrantScope = "document" if directory.is_relative_to(home) else "device"
    return GrantSuggestion(scope, grant_spelling(directory, home))


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
