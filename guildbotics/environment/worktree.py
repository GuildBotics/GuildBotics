"""A working directory a command works on a copy of, on the host's side.

The command's microVM copies the read-only host directory onto its own disk
when it boots (:func:`copy_worktree`), and tells the host what the command
changed there when it ends (:func:`write_back`), both with
:mod:`guildbotics.guest.worktree_copy` run inside it. The host never opens
the copy. It receives the changes as data, into a temporary directory of its
own, and writes back regular files only: every path is checked before
anything is written, each file is reached from the working directory
without following a link, and a file that changed on the host while the
command ran is not overwritten -- then nothing is written back.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
import sys
import threading
import time
import unicodedata
from collections.abc import Iterator
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from guildbotics.commands.errors import CommandError
from guildbotics.environment.command_guest import EnvironmentGuest
from guildbotics.environment.runtime import AgentEnvironment
from guildbotics.environment.spec import (
    WORKTREE_SOURCE,
    WorktreeCopy,
)
from guildbotics.intelligences.agent_runtime.wire import (
    MAX_WORKTREE_CHANGE_BYTES,
    MAX_WORKTREE_LIST_BYTES,
    ChangedFile,
    CopiedFile,
)
from guildbotics.runtime.member_invocation import GuestProcessError
from guildbotics.utils.advisory_lock import LockTimeoutError, held_lock
from guildbotics.utils.async_utils import finish_on_cancel
from guildbotics.utils.fileio import get_machine_state_path, host_temporary_directory
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.log_utils import get_logger
from guildbotics.utils.safe_paths import (
    UnsafePathError,
    host_directory_case_sensitive,
    inspect_host_path,
    visit_host_tree,
)

_MODULE = "guildbotics.guest.worktree_copy"
#: How long a write-back waits for another one on this device.
_WRITE_BACK_WAIT_SECONDS = 600.0
#: How often a waiting write-back looks whether its command was cancelled.
_LOCK_POLL_SECONDS = 0.5
_CHUNK_BYTES = 1 << 20
#: Never a link, and on Windows never text: what is written is the bytes.
_OPEN = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
_COPIED = TypeAdapter(CopiedFile)
_CHANGED = TypeAdapter(ChangedFile)


@dataclass(frozen=True)
class Worktree:
    """A copy the command works on: where it came from, and what was copied."""

    copy: WorktreeCopy
    guest: str
    copied: dict[str, CopiedFile]


async def copy_worktree(
    environment: AgentEnvironment, copy: WorktreeCopy, guest: str
) -> Worktree:
    """Copy the read-only working directory to ``guest`` in the microVM.

    Raises:
        CommandError: When it cannot be copied.
    """
    runner = _guest(environment)
    try:
        result = await runner.run_here(
            runner.python(_MODULE, "copy", WORKTREE_SOURCE, guest, *copy.excluded),
            cwd="/",
            env={},
            stdout_limit=MAX_WORKTREE_LIST_BYTES,
        )
    except GuestProcessError as exc:
        raise _copy_failed(str(exc)) from exc
    if result.returncode != 0:
        raise _copy_failed(result.stderr.decode(errors="replace").strip())
    copied = (_COPIED.validate_json(line) for line in result.stdout.splitlines())
    return Worktree(copy, guest, {each["path"]: each for each in copied})


async def write_back(environment: AgentEnvironment, worktree: Worktree) -> None:
    """Write what the command changed in its copy back to the host directory.

    Raises:
        CommandError: When the changes cannot be collected, a change is not
            one of a regular file inside the directory, or a file changed on
            the host while the command ran; nothing is written back then.
    """
    with host_temporary_directory("guildbotics-worktree-") as held:
        changes = Path(held) / "changes.jsonl"
        listed = "".join(json.dumps(each) + "\n" for each in worktree.copied.values())
        guest = _guest(environment)
        try:
            result = await guest.run_here(
                guest.python(
                    _MODULE, "changes", worktree.guest, *worktree.copy.excluded
                ),
                cwd="/",
                env={},
                stdin=listed.encode(),
                stdout=changes,
                stdout_limit=MAX_WORKTREE_CHANGE_BYTES,
            )
        except GuestProcessError as exc:
            raise _collect_failed(str(exc)) from exc
        if result.returncode != 0:
            raise _collect_failed(result.stderr.decode(errors="replace").strip())
        # Waiting for another write-back and hashing the files take as long
        # as they take; the command's loop answers its microVM meanwhile.
        cancelled = threading.Event()
        work = asyncio.ensure_future(
            asyncio.to_thread(_write, worktree, changes, cancelled)
        )
        try:
            await finish_on_cancel(work, on_cancel=cancelled.set)
        except asyncio.CancelledError:
            _log_cancelled(worktree, work)
            raise


def _log_cancelled(worktree: Worktree, work: asyncio.Future[None]) -> None:
    """Log what a cancelled command's write-back did after all: the command
    ends as cancelled, so a write-back that put files in place, or failed
    otherwise than by stopping, is told nowhere else."""
    if work.cancelled():
        return
    error = work.exception()
    if error is None:
        get_logger().warning(
            "The cancelled command's changes were written back to %s.",
            worktree.copy.host,
        )
    elif not isinstance(error.__cause__, _Cancelled):
        get_logger().error("The cancelled command's write-back failed: %s", error)


class _Cancelled(ValueError):
    """The command was cancelled before anything was put in place: what was
    staged is discarded as for any refusal."""


def _write(worktree: Worktree, changes: Path, cancelled: threading.Event) -> None:
    """Check every change and stage every file beside its place, then put
    them in place: a write that cannot be made leaves nothing written.

    ``cancelled`` stops it at the next step until files are put in place;
    from then on it finishes, so it never leaves a part of them.

    One write-back at a time on this device, from checking to putting in
    place, across processes -- the working directories of two commands may
    hold one another: one that comes later checks against what the earlier
    wrote, and so refuses it as a change made meanwhile.

    Raises:
        CommandError: For a change the host may not write, a file that
            changed on the host while the command ran, or a file that could
            not be written; or, when putting the staged files in place
            failed, naming the ones that were.
    """
    root, excluded = worktree.copy.host, worktree.copy.excluded
    staged: list[tuple[PurePosixPath, str | None]] = []
    with ExitStack() as held:
        try:
            _hold_write_back_lock(held, cancelled)
            if inspect_host_path(root).identities[-1] != worktree.copy.identity:
                raise UnsafePathError(f"'{root}' was replaced while the command ran.")
            case_sensitive = host_directory_case_sensitive(root)
            conflicts: list[str] = []
            for change in _changes(changes, excluded, case_sensitive):
                _go_on(cancelled)
                if not _unchanged(root, change, worktree.copied):
                    conflicts.append(change["path"])
            if not conflicts:
                for change in _changes(changes, excluded, case_sensitive):
                    _go_on(cancelled)
                    path = PurePosixPath(change["path"])
                    staged.append((path, _stage(root, path, change)))
                _go_on(cancelled)
        except (OSError, ValueError) as exc:
            _discard(root, staged)
            raise CommandError(
                t(
                    "intelligences.agent_environment.runtime.worktree_refused",
                    path=root,
                    reason=exc,
                )
            ) from exc
        if conflicts:
            raise CommandError(
                t(
                    "intelligences.agent_environment.runtime.worktree_conflict",
                    path=root,
                    paths=", ".join(conflicts),
                )
            )
        written: list[str] = []
        try:
            for path, temporary in staged:
                _put(root, path, temporary)
                written.append(path.as_posix())
        except (OSError, ValueError) as exc:
            _discard(root, staged[len(written) :])
            raise CommandError(
                t(
                    "intelligences.agent_environment.runtime.worktree_partial",
                    path=root,
                    paths=", ".join(written),
                    reason=exc,
                )
            ) from exc


def _hold_write_back_lock(held: ExitStack, cancelled: threading.Event) -> None:
    """Take the device's write-back lock, giving up when the command is
    cancelled meanwhile or the wait runs out.

    Raises:
        _Cancelled: When the command was cancelled.
        LockTimeoutError: When another write-back held it too long.
    """
    deadline = time.monotonic() + _WRITE_BACK_WAIT_SECONDS
    while True:
        _go_on(cancelled)
        try:
            held.enter_context(
                held_lock(
                    get_machine_state_path("run", "worktree-write-back.lock"),
                    timeout=_LOCK_POLL_SECONDS,
                )
            )
            return
        except LockTimeoutError:
            if time.monotonic() >= deadline:
                raise


def _go_on(cancelled: threading.Event) -> None:
    """Go on unless the command was cancelled.

    Raises:
        _Cancelled: When it was.
    """
    if cancelled.is_set():
        raise _Cancelled("The command was cancelled.")


def _guest(environment: AgentEnvironment) -> EnvironmentGuest:
    return EnvironmentGuest(asyncio.get_running_loop(), lambda: environment)


def _copy_failed(error: str) -> CommandError:
    return CommandError(
        t("intelligences.agent_environment.runtime.worktree_copy_failed", error=error)
    )


def _collect_failed(error: str) -> CommandError:
    return CommandError(
        t(
            "intelligences.agent_environment.runtime.worktree_changes_failed",
            error=error,
        )
    )


def _changes(
    path: Path, excluded: tuple[str, ...], case_sensitive: bool
) -> Iterator[ChangedFile]:
    """The changes the microVM told, each a regular file inside the
    directory and outside its ``.git`` and the mounts nested in it, and each
    of a file of its own on the host -- where names differing in case or
    Unicode form are one file, never two such names.

    Raises:
        ValueError: For a change that is not.
    """
    seen: set[str] = set()
    with path.open("rb") as lines:
        for line in lines:
            try:
                change = _CHANGED.validate_json(line)
            except ValidationError as exc:
                raise ValueError(f"Unreadable change: {exc}") from exc
            name = change["path"]
            _check_name(name, excluded, case_sensitive)
            key = _host_name(name, case_sensitive)
            if key in seen:
                raise ValueError(f"'{name}' is changed twice.")
            seen.add(key)
            if change.get("deleted") == ("content" in change):
                raise ValueError(f"'{name}' is neither written nor deleted.")
            yield change


def _host_name(name: str, case_sensitive: bool) -> str:
    """The name as the host's file system tells files apart: where it does
    not by case or Unicode form, every spelling of one file is one name."""
    return name if case_sensitive else unicodedata.normalize("NFD", name).casefold()


def _check_name(name: str, excluded: tuple[str, ...], case_sensitive: bool) -> None:
    path = PurePosixPath(name)
    parts = path.parts
    if (
        not parts
        or path.is_absolute()
        or path.as_posix() != name
        or any(
            part in {".", ".."}
            or part.casefold() == ".git"
            or "\\" in part
            or "\0" in part
            for part in parts
        )
        or (os.name == "nt" and not all(map(_windows_name, parts)))
    ):
        raise ValueError(f"'{name}' is not a file inside the working directory.")
    host = PurePosixPath(_host_name(name, case_sensitive))
    if any(host.is_relative_to(_host_name(each, case_sensitive)) for each in excluded):
        raise ValueError(f"'{name}' is inside a directory mounted on its own.")


#: Names Windows takes for a device, with any extension.
_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{kind}{n}" for kind in ("COM", "LPT") for n in (*"123456789", "¹", "²", "³")}
)


def _windows_name(part: str) -> bool:
    """Whether Windows keeps ``part`` as the name of a file of its own: not a
    stream, not a device, and not one it trims into another name."""
    return not (
        ":" in part
        or part.endswith((".", " "))
        or part.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_DEVICES
    )


def _unchanged(root: Path, change: ChangedFile, copied: dict[str, CopiedFile]) -> bool:
    """Whether the host file is still what was copied, as far as the change
    writes it: the same content -- or still absent when it was not copied --
    and, when the change sets the executable bit, the same bit."""
    path = PurePosixPath(change["path"])
    before = copied.get(change["path"])
    found: list[tuple[str, bool] | None] = []

    def read(fd: int | None) -> None:
        found.append(_state(fd, root.joinpath(*path.parts), path.name))

    visit_host_tree(root, path.parent, read, create=False)
    now = found[0] if found else None
    if before is None or now is None:
        return before is None and now is None
    return now[0] == before["sha256"] and (
        "executable" not in change
        or sys.platform == "win32"
        or now[1] == before["executable"]
    )


def _state(fd: int | None, full: Path, name: str) -> tuple[str, bool] | None:
    """The leaf's content digest and executable bit, or None when there is
    none.

    Raises:
        UnsafePathError: For a leaf that is not a regular file.
    """
    info = _regular(fd, full, name)
    if info is None:
        return None
    handle = os.open(full if fd is None else name, os.O_RDONLY | _OPEN, dir_fd=fd)
    digest = hashlib.sha256()
    with os.fdopen(handle, "rb") as file:
        while chunk := file.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest(), bool(info.st_mode & 0o111)


def _stage(root: Path, path: PurePosixPath, change: ChangedFile) -> str | None:
    """Write a changed file's content beside its place under a temporary
    name, keeping the mode of the file it replaces but its executable bit;
    nothing for a deletion.

    Returns:
        The temporary name, or None for a deletion.
    """
    if change.get("deleted"):
        return None
    staged: list[str] = []

    def stage(fd: int | None) -> None:
        info = _regular(fd, root.joinpath(*path.parts), path.name)
        executable = change.get("executable")
        name = f".{path.name}.guildbotics-{uuid4().hex}"
        at = root.joinpath(*path.parent.parts, name) if fd is None else name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN
        handle = os.open(at, flags, 0o777 if executable else 0o666, dir_fd=fd)
        staged.append(name)
        with os.fdopen(handle, "wb") as file:
            file.write(base64.b64decode(change.get("content", "")))
            if info is not None and sys.platform != "win32":
                mode = stat.S_IMODE(info.st_mode)
                if executable is not None:
                    mode = mode | (mode & 0o444) >> 2 if executable else mode & ~0o111
                os.fchmod(file.fileno(), mode)

    try:
        visit_host_tree(root, path.parent, stage, create=True)
    except BaseException:
        _discard(root, [(path, staged[0])] if staged else [])
        raise
    return staged[0]


def _put(root: Path, path: PurePosixPath, temporary: str | None) -> None:
    """Put a staged file in its place, or delete a deleted one, reached
    without following a link."""
    full = root.joinpath(*path.parts)

    def put(fd: int | None) -> None:
        at = full if fd is None else path.name
        if temporary is None:
            if _regular(fd, full, path.name) is not None:
                os.unlink(at, dir_fd=fd)
            return
        source = full.with_name(temporary) if fd is None else temporary
        os.replace(source, at, src_dir_fd=fd, dst_dir_fd=fd)

    if not visit_host_tree(root, path.parent, put, create=False) and temporary:
        raise FileNotFoundError(f"'{full.parent}' is gone.")


def _discard(root: Path, staged: list[tuple[PurePosixPath, str | None]]) -> None:
    """Remove the staged files not put in place."""
    for path, temporary in staged:
        if temporary is None:
            continue
        at = root.joinpath(*path.parent.parts, temporary)

        def remove(fd: int | None, name: str = temporary, at: Path = at) -> None:
            os.unlink(at if fd is None else name, dir_fd=fd)

        with suppress(OSError, ValueError):
            visit_host_tree(root, path.parent, remove, create=False)


def _regular(fd: int | None, full: Path, name: str) -> os.stat_result | None:
    """The leaf, which must be a regular file when there is one.

    Raises:
        UnsafePathError: For a leaf that is not a regular file.
    """
    try:
        info = os.stat(full if fd is None else name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or _reparse(info):
        raise UnsafePathError(f"'{full}' is not a regular file.")
    return info


def _reparse(info: os.stat_result) -> bool:
    """A Windows reparse point (a junction or a link) is no regular file."""
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)
