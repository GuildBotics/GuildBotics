"""Inspect and create paths without traversing links."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


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
    return t("intelligences.agent_environment.filesystem.permission_denied", path=path)


def normalize_host_path(path: Path) -> Path:
    """Expand a name, without accessing its filesystem objects."""
    path = path.expanduser().absolute()
    if ".." in path.parts or "\x00" in str(path):
        raise UnsafePathError(t("safe_paths.invalid", path=path))
    return path


@dataclass(frozen=True)
class PathFacts:
    """Opened object identities, followed by components that do not exist."""

    path: Path
    identities: tuple[tuple[int, int], ...]
    missing: tuple[str, ...] = ()

    @property
    def present(self) -> bool:
        return not self.missing


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
        path: Absolute path to inspect.
        create: Create missing directories relative to checked parent handles.
        missing: Return the existing ancestry of a missing name.
        directory: Require a directory; otherwise also permit a regular file.
    """
    path = normalize_host_path(path)
    try:
        identities, absent = _inspect_posix(
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
    return PathFacts(path, identities, absent)


def _inspect_posix(
    path: Path,
    create: bool,
    directory: bool,
    link_as_missing: bool = False,
) -> tuple[tuple[tuple[int, int], ...], tuple[str, ...]]:
    descriptors: list[int] = []
    identities: list[tuple[int, int]] = []
    parts = path.parts[1:]
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    checked = Path(path.anchor)
    try:
        parent = os.open(path.anchor, flags | os.O_DIRECTORY)
        descriptors.append(parent)
        identities.append(_identity(os.fstat(parent)))
        for index, part in enumerate(parts):
            checked = Path(path.anchor).joinpath(*parts[: index + 1])
            is_directory = directory or index < len(parts) - 1
            try:
                info = os.stat(part, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    if link_as_missing:
                        return tuple(identities), parts[index:]
                    raise UnsafePathError(t("safe_paths.link", path=path))
                handle = os.open(
                    part, flags | (os.O_DIRECTORY if is_directory else 0), dir_fd=parent
                )
            except FileNotFoundError:
                if not create:
                    return tuple(identities), parts[index:]
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=parent)
                handle = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            descriptors.append(handle)
            info = os.fstat(handle)
            if not (
                stat.S_ISDIR(info.st_mode)
                or (not is_directory and stat.S_ISREG(info.st_mode))
            ):
                raise OSError(f"Not a directory or regular file: {part}")
            identities.append(_identity(info))
            parent = handle
        return tuple(identities), ()
    except PermissionError as exc:
        raise HostPathPermissionError(checked) from exc
    finally:
        for handle in reversed(descriptors):
            os.close(handle)


def _identity(info: os.stat_result) -> tuple[int, int]:
    if not info.st_ino:
        raise OSError("The filesystem supplies no object identity")
    return info.st_dev, info.st_ino
