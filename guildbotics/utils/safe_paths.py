"""Inspect and create host paths without traversing links.

Only fixed operating-system aliases are translated. Every filesystem access
then opens one component relative to the preceding directory handle. The
checked spelling is also the spelling passed to the microVM runtime.
"""

from __future__ import annotations

import os
import stat
import sys
import unicodedata
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any

# These APIs exist only on POSIX; callers select this branch before using them.
_POSIX: Any = os
MAX_HOST_LINKS = 40


def t(key: str, **values: object) -> str:
    # Workspace selection also uses this module during i18n initialization.
    from guildbotics.utils import i18n_tool

    return i18n_tool.t(key, **values)


class UnsafePathError(ValueError):
    """A host path cannot be inspected without following links."""


class HostPathPermissionError(UnsafePathError):
    """A localized permission refusal, still caught as an unsafe host path."""

    def __init__(self, path: Path, *, protected: Path | None = None) -> None:
        self.filename = str(normalize_host_path(path))
        reason = filesystem_permission_problem(Path(self.filename))
        super().__init__(
            t("safe_paths.protected_unavailable", protected=protected, reason=reason)
            if protected is not None
            else reason
        )


def filesystem_permission_problem(path: Path) -> str:
    """One permission message for CLI, command execution, and Desktop."""
    from guildbotics.utils.processes import launching_app_name

    if sys.platform == "darwin" and path.is_relative_to(Path.home() / "Documents"):
        app = launching_app_name()
        return t(
            "intelligences.agent_environment.filesystem.macos_documents",
            path=path,
            app=t("intelligences.agent_environment.filesystem.launching_app", app=app)
            if app
            else "",
        )
    return t("intelligences.agent_environment.filesystem.permission_denied", path=path)


def normalize_host_path(path: Path) -> Path:
    """Expand a name, without accessing its filesystem objects."""
    path = path.expanduser().absolute()
    if ".." in path.parts or "\x00" in str(path):
        raise UnsafePathError(t("safe_paths.invalid", path=path))
    if os.name == "nt" and (
        not path.drive.endswith(":")
        or any(
            ":" in component or component.endswith((".", " "))
            for component in path.parts[1:]
        )
    ):
        raise UnsafePathError(t("safe_paths.invalid", path=path))
    return _host_alias(path)


def _host_alias(path: Path) -> Path:
    if sys.platform == "darwin":
        for alias in ("/tmp", "/var", "/etc"):
            if path.is_relative_to(alias):
                return Path("/private") / path.relative_to("/")
    return path


@dataclass(frozen=True)
class HostPathResolution:
    """All names traversed before no-follow inspection, including link loops."""

    path: Path
    names: tuple[Path, ...]
    cyclic: bool = False


def resolve_host_links(
    path: Path, *, on_link: Callable[[Path, bool], None] | None = None
) -> HostPathResolution:
    """Walk links explicitly, retaining ordinary drive spelling and every hop.

    Admit each link before reading its target. The resulting file must still
    be opened without following links, using the held ancestry.
    """
    path = normalize_host_path(path)
    names = [path]
    links = 0
    pending = deque(path.parts[1:])
    current = Path(path.anchor)
    while pending:
        component = pending.popleft()
        if component == "..":
            current = current.parent
            continue
        candidate = current / component
        leaf = not pending
        try:
            info = candidate.lstat()
            linked = stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
            if not linked:
                if not leaf and not stat.S_ISDIR(info.st_mode):
                    raise NotADirectoryError(str(candidate))
                inspect_host_path(candidate, directory=not leaf)
                current = candidate
                continue
            names.append(candidate)
            links += 1
            if links > MAX_HOST_LINKS:
                return HostPathResolution(candidate, tuple(dict.fromkeys(names)), True)
            if on_link is not None:
                on_link(candidate, leaf)
            target = os.readlink(candidate)
            if os.name == "nt" and target.startswith("\\\\?\\"):
                target = target[4:]
                if not Path(target).drive.endswith(":"):
                    raise UnsafePathError(t("safe_paths.invalid", path=target))
            destination = Path(target)
            if not destination.is_absolute():
                destination = candidate.parent / destination
            destination = _host_alias(destination)
            current = Path(destination.anchor)
            pending = deque((*destination.parts[1:], *pending))
        except (FileNotFoundError, NotADirectoryError) as exc:
            # Keep the untraversed tail for admission to refuse. Only the
            # inspected prefix belongs in the protected path's facts.
            untraversable = isinstance(exc, NotADirectoryError) or ".." in pending
            if untraversable:
                names.append(candidate)
            current = candidate
            for component in pending:
                current = current / component
            if untraversable:
                return HostPathResolution(current, tuple(dict.fromkeys(names)))
            break
        except PermissionError as exc:
            raise HostPathPermissionError(candidate) from exc
    names.append(current)
    return HostPathResolution(current, tuple(dict.fromkeys(names)))


@dataclass(frozen=True)
class PathFacts:
    """Opened object identities, followed by components that do not exist."""

    path: Path
    identities: tuple[tuple[int, int], ...]
    missing: tuple[str, ...] = ()
    case_sensitive: bool = True

    @property
    def present(self) -> bool:
        return not self.missing

    def contains(self, other: PathFacts) -> bool:
        """Compare existing ancestors by identity, never by path spelling."""
        if self.present:
            return self.identities[-1] in other.identities
        if self.identities[-1] != other.identities[-1]:
            return False

        def names(parts: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(
                p
                if self.case_sensitive and other.case_sensitive
                else unicodedata.normalize("NFD", p).casefold()
                for p in parts
            )

        return names(other.missing[: len(self.missing)]) == names(self.missing)


def inspect_host_path(
    path: Path,
    *,
    create: bool = False,
    missing: bool = False,
    directory: bool = True,
    link_as_missing: bool = False,
) -> PathFacts:
    """Open every component without following links, optionally making dirs.

    Args:
        path: Host name. Only known OS aliases may change its spelling.
        create: Create missing directories relative to checked parent handles.
        missing: Return the existing ancestry of a missing name.
        directory: Require a directory; otherwise also permit a regular file.
    """
    path = normalize_host_path(path)
    try:
        if os.name == "nt":
            from guildbotics.utils.safe_paths_windows import inspect_windows_path

            identities, absent = inspect_windows_path(
                path, create, directory, link_as_missing=link_as_missing
            )
            case_sensitive = False
        else:
            identities, absent, case_sensitive = _inspect_posix(
                path, create, directory, link_as_missing=link_as_missing
            )
    except PermissionError as exc:
        raise HostPathPermissionError(path) from exc
    except OSError as exc:
        raise UnsafePathError(
            t("safe_paths.unavailable", path=path, reason=exc)
        ) from exc
    if absent and not missing:
        raise UnsafePathError(t("safe_paths.missing", path=path))
    return PathFacts(path, identities, absent, case_sensitive)


def _inspect_posix(
    path: Path,
    create: bool,
    directory: bool,
    consume: Callable[[int], None] | None = None,
    open_file: bool = False,
    ancestor_of: PathFacts | None = None,
    link_as_missing: bool = False,
) -> tuple[tuple[tuple[int, int], ...], tuple[str, ...], bool]:
    descriptors: list[int] = []
    identities: list[tuple[int, int]] = []
    parts = path.parts[1:]
    flags = os.O_RDONLY | _POSIX.O_NOFOLLOW | _POSIX.O_NONBLOCK
    checked = Path(path.anchor)
    try:
        parent = os.open(path.anchor, flags | _POSIX.O_DIRECTORY)
        descriptors.append(parent)
        identities.append(_identity(os.fstat(parent)))
        for index, part in enumerate(parts):
            checked = Path(path.anchor).joinpath(*parts[: index + 1])
            is_directory = directory or index < len(parts) - 1
            leaf_file = open_file and index == len(parts) - 1
            try:
                try:
                    info = os.stat(part, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    if not leaf_file:
                        raise
                    info = None
                if info is not None and stat.S_ISLNK(info.st_mode):
                    if link_as_missing:
                        return tuple(identities), parts[index:], False
                    raise UnsafePathError(t("safe_paths.link", path=path))
                if ancestor_of is not None:
                    assert info is not None
                    identity = _identity(info)
                    if identity not in ancestor_of.identities:
                        # This object cannot contain the independently opened
                        # target. No need to open an unrelated private folder.
                        return (*identities, identity), (), True
                handle = os.open(
                    part,
                    (os.O_RDWR | os.O_CREAT | _POSIX.O_NOFOLLOW | _POSIX.O_NONBLOCK)
                    if leaf_file
                    else flags | (_POSIX.O_DIRECTORY if is_directory else 0),
                    0o600,
                    dir_fd=parent,
                )
            except FileNotFoundError:
                if not create:
                    # Darwin reports the opened volume's case rules. Elsewhere
                    # absent protected names are compared conservatively.
                    sensitive = (
                        _POSIX.fpathconf(parent, 11) if sys.platform == "darwin" else 0
                    )
                    if sensitive < 0:
                        raise OSError(
                            "The filesystem supplies no case-sensitivity information"
                        ) from None
                    return tuple(identities), parts[index:], bool(sensitive)
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=parent)
                handle = os.open(part, flags | _POSIX.O_DIRECTORY, dir_fd=parent)
            descriptors.append(handle)
            info = os.fstat(handle)
            if not (
                stat.S_ISDIR(info.st_mode)
                or (not is_directory and stat.S_ISREG(info.st_mode))
            ):
                raise OSError(f"Not a directory or regular file: {part}")
            identities.append(_identity(info))
            parent = handle
        if consume is not None:
            consume(parent)
        return tuple(identities), (), True
    except PermissionError as exc:
        raise HostPathPermissionError(checked) from exc
    finally:
        for handle in reversed(descriptors):
            os.close(handle)


def _identity(info: os.stat_result) -> tuple[int, int]:
    if not info.st_ino:
        raise OSError("The filesystem supplies no object identity")
    return info.st_dev, info.st_ino


def host_path_contains(ancestor: Path, target: PathFacts) -> bool:
    """Judge ancestry by identity, stopping at the first unrelated object.

    This checks workspace admission without opening unrelated grant contents.
    Actual bind sources still require inspection of every component.
    """
    ancestor = normalize_host_path(ancestor)
    target = inspect_host_path(target.path, missing=True)
    if os.name == "nt":
        return inspect_host_path(ancestor, missing=True).contains(target)
    try:
        identities, absent, sensitive = _inspect_posix(
            ancestor, False, True, ancestor_of=target
        )
    except OSError as exc:
        if isinstance(exc, PermissionError):
            raise HostPathPermissionError(ancestor) from exc
        raise UnsafePathError(
            t("safe_paths.unavailable", path=ancestor, reason=exc)
        ) from exc
    return PathFacts(ancestor, identities, absent, sensitive).contains(target)


def read_host_file(path: Path) -> bytes:
    """Read the opened leaf, without reopening its name after inspection."""
    chunks: list[bytes] = []
    consume_host_file(path, chunks.append)
    return b"".join(chunks)


def consume_host_file(path: Path, consume: Callable[[bytes], None]) -> None:
    """Stream an opened regular leaf while its no-follow ancestry is held."""
    path = normalize_host_path(path)
    if os.name == "nt":
        from guildbotics.utils.safe_paths_windows import consume_windows_file

        try:
            consume_windows_file(path, consume)
        except PermissionError as exc:
            raise HostPathPermissionError(path) from exc
        return

    def read(handle: int) -> None:
        while chunk := os.read(handle, 65536):
            consume(chunk)

    try:
        _, missing, _ = _inspect_posix(path, False, False, read)
    except PermissionError as exc:
        raise HostPathPermissionError(path) from exc
    if missing:
        raise FileNotFoundError(str(path))


def open_host_file(path: Path) -> IO[str]:
    """Open/create a regular file relative to checked parents, never a link."""
    path = normalize_host_path(path)
    if os.name == "nt":
        from guildbotics.utils.safe_paths_windows import open_windows_file

        try:
            return open_windows_file(path)
        except PermissionError as exc:
            raise HostPathPermissionError(path) from exc
    descriptors: list[int] = []
    try:
        _inspect_posix(
            path,
            False,
            False,
            lambda fd: descriptors.append(os.dup(fd)),
            open_file=True,
        )
    except OSError as exc:
        if isinstance(exc, PermissionError):
            raise HostPathPermissionError(path) from exc
        raise UnsafePathError(
            t("safe_paths.unavailable", path=path, reason=exc)
        ) from exc
    if not descriptors:
        raise UnsafePathError(t("safe_paths.missing", path=path))
    return os.fdopen(descriptors[0], "r+", encoding="utf-8")


def visit_host_directory(path: Path, action: Callable[[int | None], None]) -> None:
    """Keep checked ancestors open while acting on a private directory.

    POSIX actions receive a directory descriptor for relative operations.
    Windows keeps non-delete-shared handles open, preventing ancestor swaps.
    """
    path = normalize_host_path(path)
    if os.name == "nt":
        from guildbotics.utils.safe_paths_windows import inspect_windows_path

        try:
            _, absent = inspect_windows_path(
                path, False, True, lambda _: action(None), stable=True
            )
        except PermissionError as exc:
            raise HostPathPermissionError(path) from exc
    else:

        def consume(fd: int) -> None:
            _POSIX.fchmod(fd, 0o700)
            action(fd)

        try:
            _, absent, _ = _inspect_posix(path, False, True, consume)
        except PermissionError as exc:
            raise HostPathPermissionError(path) from exc
    if absent:
        raise UnsafePathError(t("safe_paths.missing", path=path))


def visit_host_tree(
    root: Path,
    relative: PurePosixPath,
    action: Callable[[int | None], None],
    *,
    create: bool,
) -> bool:
    """Act in the directory ``relative`` under ``root`` while its no-follow
    ancestry from the filesystem root is held.

    Unlike :func:`visit_host_directory`, this is for a directory of the
    user's own: what it makes (when ``create``) takes the default
    permissions, and nothing is made private. POSIX actions receive a
    directory descriptor for relative operations; Windows keeps
    non-delete-shared handles open instead.

    Returns:
        Whether the directory was there to act in; without ``create``, a
        missing one is not acted in.
    """
    root = normalize_host_path(root)
    target = root.joinpath(*relative.parts)
    acted: list[None] = []

    def act(fd: int | None) -> None:
        action(fd)
        acted.append(None)

    if os.name == "nt":
        from guildbotics.utils.safe_paths_windows import inspect_windows_path

        try:
            _, absent = inspect_windows_path(
                target, create, True, lambda _: act(None), stable=True
            )
        except PermissionError as exc:
            raise HostPathPermissionError(target) from exc
        if len(absent) > len(relative.parts):
            raise UnsafePathError(t("safe_paths.missing", path=root))
        return bool(acted)

    def descend(parent: int) -> None:
        handles: list[int] = []
        try:
            for part in relative.parts:
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, dir_fd=parent)
                try:
                    parent = os.open(
                        part,
                        os.O_RDONLY | _POSIX.O_NOFOLLOW | _POSIX.O_DIRECTORY,
                        dir_fd=parent,
                    )
                except FileNotFoundError:
                    return
                except PermissionError:
                    raise
                except OSError as exc:
                    raise UnsafePathError(
                        t("safe_paths.unavailable", path=target, reason=exc)
                    ) from exc
                handles.append(parent)
            act(parent)
        finally:
            for handle in reversed(handles):
                os.close(handle)

    try:
        _, absent, _ = _inspect_posix(root, False, True, descend)
    except PermissionError as exc:
        raise HostPathPermissionError(target) from exc
    if absent:
        raise UnsafePathError(t("safe_paths.missing", path=root))
    return bool(acted)


def host_directory_case_sensitive(path: Path) -> bool:
    """Whether the file system holding the directory tells names apart by
    case. Darwin is asked of the directory itself, as it answers per volume;
    Windows never does; elsewhere there is no portable question, and the
    volume is taken to (a case-insensitive one mounted on Linux is not told).

    Raises:
        OSError: When Darwin's volume does not answer.
    """
    if os.name == "nt":
        return False
    if sys.platform != "darwin":
        return True
    answers: list[int] = []
    visit_host_tree(
        path,
        PurePosixPath(),
        lambda fd: answers.append(_POSIX.fpathconf(fd, 11)),
        create=False,
    )
    if answers[0] < 0:
        raise OSError(f"'{path}' supplies no case-sensitivity information")
    return answers[0] > 0
