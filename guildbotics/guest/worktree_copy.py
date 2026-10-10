"""Copy a working directory inside a command's microVM, and tell what changed.

A command that may write but is run in a directory no grant opens works on a
copy of it (``environment.spec.WorktreeCopy``): the host directory is
mounted read-only, and this module, run there (``python -m``), copies it onto
the microVM's own disk and later tells the host what the command changed.
The host never reads or writes the copy; it receives the changes as data and
writes them back itself, so nothing the command leaves there -- a hook, a
link, a configuration git would follow -- reaches the host.

Only directories and regular files are copied, and only regular files are
reported; ``.git`` is neither copied nor reported. The copy gets a gitfile
naming the read-only original's ``.git``, so ``git status`` and ``git diff``
work while nothing can be committed.

``copy <source> <destination> [<excluded>...]`` writes one JSON line per file
copied (:class:`CopiedFile`). ``changes <destination> [<excluded>...]`` reads
those lines on standard input and writes one JSON line per file changed
(:class:`ChangedFile`). ``excluded`` are directories under the copy, relative
to it, that are mounts of their own.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

from guildbotics.intelligences.agent_runtime.wire import (
    ChangedFile,
    CopiedFile,
)

_GIT = ".git"
_CHUNK_BYTES = 1 << 20


class WorktreeCopyError(ValueError):
    """A copy whose changes cannot be told; the message says why."""


def copy_tree(
    source: Path, destination: Path, excluded: frozenset[str]
) -> Iterator[CopiedFile]:
    """Copy the directories and regular files of ``source`` to ``destination``."""
    destination.mkdir(parents=True, exist_ok=True)
    for path, info in _walk(source, excluded):
        target = destination / path
        if stat.S_ISDIR(info.st_mode):
            target.mkdir(exist_ok=True)
            continue
        shutil.copyfile(source / path, target, follow_symlinks=False)
        executable = _executable(info)
        os.chmod(target, 0o755 if executable else 0o644)
        yield CopiedFile(path=path, sha256=_sha256(target), executable=executable)
    if (source / _GIT).is_dir() and not (source / _GIT).is_symlink():
        (destination / _GIT).write_text(f"gitdir: {source / _GIT}\n", encoding="utf-8")


def changed_files(
    destination: Path, copied: dict[str, CopiedFile], excluded: frozenset[str]
) -> Iterator[ChangedFile]:
    """The regular files of the copy that differ from what was ``copied``.

    A link or anything else the command made is no file to write back and is
    left out; a copied file that became one cannot be told.

    Raises:
        WorktreeCopyError: For a copied file that is no regular file anymore.
    """
    present: set[str] = set()
    for path, info in _walk(destination, excluded):
        if stat.S_ISDIR(info.st_mode):
            continue
        present.add(path)
        before = copied.get(path)
        executable = _executable(info)
        digest = _sha256(destination / path)
        if (
            before is not None
            and before["sha256"] == digest
            and before["executable"] == executable
        ):
            continue
        changed = ChangedFile(
            path=path,
            content=base64.b64encode((destination / path).read_bytes()).decode(),
        )
        if before is None or before["executable"] != executable:
            changed["executable"] = executable
        yield changed
    for path in sorted(copied.keys() - present):
        if os.path.lexists(destination / path):
            raise WorktreeCopyError(f"'{path}' is no longer a regular file.")
        yield ChangedFile(path=path, deleted=True)


def _walk(root: Path, excluded: frozenset[str]) -> Iterator[tuple[str, os.stat_result]]:
    """The directories and regular files under ``root`` by their relative
    paths, parents first, never following a link and never into ``.git``."""
    pending = [""]
    while pending:
        directory = pending.pop()
        with os.scandir(root / directory) as entries:
            for entry in sorted(entries, key=lambda each: each.name):
                path = f"{directory}/{entry.name}" if directory else entry.name
                if entry.name.casefold() == _GIT or path in excluded:
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    yield path, info
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    yield path, info


def _executable(info: os.stat_result) -> bool:
    return bool(info.st_mode & 0o111)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    """Run ``copy`` or ``changes``; a failure is told on standard error."""
    out = sys.stdout
    try:
        match argv:
            case ["copy", source, destination, *excluded]:
                for copied in copy_tree(
                    Path(source), Path(destination), frozenset(excluded)
                ):
                    out.write(json.dumps(copied) + "\n")
            case ["changes", destination, *excluded]:
                listed: dict[str, CopiedFile] = {}
                for line in sys.stdin:
                    each: CopiedFile = json.loads(line)
                    listed[each["path"]] = each
                for changed in changed_files(
                    Path(destination), listed, frozenset(excluded)
                ):
                    out.write(json.dumps(changed) + "\n")
            case _:
                raise WorktreeCopyError(f"Unknown arguments: {argv}")
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
