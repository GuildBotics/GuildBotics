from __future__ import annotations

import os
import shutil
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal
from uuid import uuid4

from fastapi import UploadFile

from guildbotics.app_api.errors import AppApiError
from guildbotics.intelligences.agent_environment.contract import (
    AccessContractError,
    LocalGrants,
    SharedGrants,
    exchange_tmp_dir,
    grant_spelling,
    load_local_grants,
    load_shared_grants,
    protected_paths,
    resolve_access,
    validate_mount_source,
)
from guildbotics.intelligences.agent_environment.spec import guest_path
from guildbotics.utils.advisory_lock import (
    lock_file_nonblocking,
    unlock_file,
)
from guildbotics.utils.fileio import WorkspaceNotConfiguredError
from guildbotics.utils.safe_paths import (
    HostPathPermissionError,
    UnsafePathError,
    consume_host_file,
    inspect_host_path,
    normalize_host_path,
    open_host_file,
    resolve_host_links,
    t,
    visit_host_directory,
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
        from guildbotics.intelligences.agent_environment.contract import (
            protected_paths,
            validate_mount_source,
        )

        self._root = validate_mount_source(
            self._root, protected_paths(), grant=True, create=True
        )
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

    def save(self, upload_file: UploadFile) -> Path:
        """Save an image in the active App API session directory."""
        return save_command_input_file(self._session_directory(), upload_file)

    def copy(self, source: Path, cwd: Path | None = None) -> Path:
        """Copy a file of the user's into the active session directory."""
        return copy_command_input_file(self._session_directory(), source, cwd)

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


def copy_command_input_file(
    directory: Path, source: Path, cwd: Path | None = None
) -> Path:
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
    name = source.name
    source = _admit_input_path(source, cwd)
    try:
        info = source.stat()
    except PermissionError as exc:
        raise HostPathPermissionError(source) from exc
    except FileNotFoundError as exc:
        raise ValueError(f"'{source}' is not a file.") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"'{source}' is not a file.")
    _check_size(info.st_size)

    def write(output: IO[bytes]) -> int:
        size = 0

        def chunk(content: bytes) -> None:
            nonlocal size
            size += len(content)
            _check_size(size)
            output.write(content)

        consume_host_file(source, chunk)
        return size

    return _write_command_input_file(directory, f"{uuid4().hex[:8]}-{name}", write)


def _input_grants() -> tuple[SharedGrants, LocalGrants]:
    try:
        return load_shared_grants(), load_local_grants()
    except WorkspaceNotConfiguredError:
        return SharedGrants(), LocalGrants()


def _admit_input_path(source: Path, cwd: Path | None) -> Path:
    """A copy must never borrow the host's authority through a turn's link."""
    denied = protected_paths()

    def admit_link(link: Path, leaf: bool) -> None:
        if leaf:
            raise UnsafePathError(t("safe_paths.link", path=link))
        parent = inspect_host_path(link.parent)
        if any(facts.contains(parent) for closed in denied for facts in closed.facts()):
            raise UnsafePathError(t("safe_paths.link", path=link))
        # A current cwd cannot prove that a previous turn never wrote here.
        # Trust only a parent that can never be granted: it contains a fixed
        # credential/device path. Registered workspace state is removable.
        if not any(
            parent.contains(
                inspect_host_path(
                    closed.path, missing=True, directory=False, link_as_missing=True
                )
            )
            for closed in denied
            if closed.kind == "credentials"
        ):
            raise UnsafePathError(t("safe_paths.link", path=link))
        shared, local = _input_grants()
        access = resolve_access(shared, local, create=False)
        writable = [
            grant.path
            for grant in (*access.documents, *access.paths)
            if grant.access == "read_write"
        ]
        if cwd is not None:
            writable.append(normalize_host_path(cwd))
        if any(
            inspect_host_path(root, missing=True).contains(parent) for root in writable
        ):
            raise UnsafePathError(t("safe_paths.link", path=link))

    resolution = resolve_host_links(source, on_link=admit_link)
    if resolution.cyclic:
        raise UnsafePathError(t("safe_paths.link", path=source))
    return validate_mount_source(resolution.path, denied, grant=True, missing=True)


def _check_size(size: int) -> None:
    if size > MAX_COMMAND_INPUT_FILE_BYTES:
        raise ValueError(
            f"File is too large (max {MAX_COMMAND_INPUT_FILE_BYTES // (1024 * 1024)} MB)."
        )


def _write_command_input_file(
    directory: Path, name: str, write: Callable[[IO[bytes]], int]
) -> Path:
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
            with os.fdopen(handle, "wb") as output:
                size = write(output)
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
    on this device would find it.

    ``guest_path`` is the spelling that goes into the field: the path as the
    agent inside the environment names it (``/c/...`` for ``C:\\...``), which
    is the only spelling it can open.
    """

    path: Path
    kind: InputPathKind
    reachable: bool
    guest_path: str
    grant: GrantSuggestion | None = None
    problem: str = ""


def command_cwd(cwd: Path | None) -> Path | None:
    """The working directory as the Desktop typed it, ready to run in.

    ``~`` is expanded, as a shell would, because the field has no shell in
    front of it. A relative path is refused: the sidecar's own directory is
    nowhere the user can see, so there is nothing to resolve it against.
    """
    if cwd is None:
        return None
    expanded = cwd.expanduser()
    if not expanded.is_absolute():
        raise AppApiError("command_cwd_not_absolute", params={"cwd": str(cwd)})
    return normalize_host_path(expanded)


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
    home = normalize_host_path(Path.home())
    described = []
    for original in paths:
        path = normalize_host_path(original)
        kind: InputPathKind = (
            "directory" if path.is_dir() else "file" if path.exists() else "missing"
        )
        try:
            reachable = access is not None and access.reaches(path, cwd)
        except AccessContractError:
            reachable = False
        grant = None
        problem = ""
        if not reachable and kind != "missing":
            try:
                real = _admit_input_path(path, cwd)
            except AccessContractError as exc:
                problem = str(exc)
            else:
                directory = real if kind == "directory" else real.parent
                with suppress(AccessContractError):
                    validate_mount_source(directory, protected_paths(), grant=True)
                    grant = _grant_for(directory, home)
        described.append(
            CommandInputPath(
                path, kind, reachable, guest_path(path.absolute()), grant, problem
            )
        )
    return described


def _grant_for(directory: Path, home: Path) -> GrantSuggestion | None:
    """The home directory itself cannot be granted; anything else can."""
    if directory == home:
        return None
    scope: GrantScope = "document" if directory.is_relative_to(home) else "device"
    return GrantSuggestion(scope, grant_spelling(directory, home))


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
