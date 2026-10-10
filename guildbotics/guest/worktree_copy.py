"""Copy a working directory inside a command's microVM, and tell what changed.

A command that may write but is run in a directory no grant opens works on a
copy of it (``environment.spec.WorktreeCopy``): the host directory is
mounted read-only, and this module, run there (``python -m``), copies it onto
the microVM's own disk and later tells the host what the command changed.
The host never reads or writes the copy; it receives the changes as data and
writes them back itself, so nothing the command leaves there -- a hook, a
link, a configuration git would follow -- reaches the host.

Only directories and regular files are copied, and only regular files are
reported; ``.git`` is neither copied nor reported. A directory whose ``.git``
is a directory is copied as far as git names it: the files it tracks and the
untracked ones it does not ignore, a directory it names (a submodule, a
nested repository) whole. Any other directory is copied whole. The copy gets
a gitfile naming the read-only original's ``.git``, so ``git status`` and
``git diff`` work while nothing can be committed. What is reported are the
changes to what was copied, and the new files git would name; the listing is
taken from the original's ``.git`` in both, whatever the command did to the
gitfile.

``copy <source> <destination> [<excluded>...]`` writes one JSON line per file
copied (:class:`CopiedFile`). ``changes <source> <destination> [<excluded>...]``
reads those lines on standard input and writes one JSON line per file changed
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
import subprocess
import sys
from collections.abc import Iterator, Set
from pathlib import Path, PurePosixPath

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
    """Copy the directories and regular files of ``source`` to ``destination``
    -- those git names, when ``source`` is a repository.

    Raises:
        WorktreeCopyError: When git cannot list the repository.
    """
    destination.mkdir(parents=True, exist_ok=True)
    repository = _repository(source)
    admitted = None if repository is None else _listed(repository, source)
    for path, info in _walk(source, excluded, admitted):
        target = destination / path
        if stat.S_ISDIR(info.st_mode):
            target.mkdir(exist_ok=True)
            continue
        shutil.copyfile(source / path, target, follow_symlinks=False)
        executable = _executable(info)
        os.chmod(target, 0o755 if executable else 0o644)
        yield CopiedFile(path=path, sha256=_sha256(target), executable=executable)
    if repository is not None:
        (destination / _GIT).write_text(f"gitdir: {repository}\n", encoding="utf-8")


def changed_files(
    source: Path,
    destination: Path,
    copied: dict[str, CopiedFile],
    excluded: frozenset[str],
) -> Iterator[ChangedFile]:
    """The regular files of the copy that differ from what was ``copied``.

    A new file counts only when git, listing the copy against ``source``'s
    repository, names it: what the command built is no change. A link or
    anything else the command made is no file to write back and is left out;
    a copied file that became one cannot be told.

    Raises:
        WorktreeCopyError: For a copied file that is no regular file anymore,
            or when git cannot list the copy.
    """
    repository = _repository(source)
    admitted = (
        None if repository is None else copied.keys() | _listed(repository, destination)
    )
    present: set[str] = set()
    for path, info in _walk(destination, excluded, admitted):
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


def _repository(root: Path) -> Path | None:
    """``root``'s ``.git``, when it is a directory git can read in the
    microVM; a gitfile names one elsewhere on the host."""
    git = root / _GIT
    return git if git.is_dir() and not git.is_symlink() else None


def _listed(repository: Path, tree: Path) -> set[str]:
    """What git names in ``tree`` with ``repository``: the files it tracks
    and the untracked ones it does not ignore, a directory it does not look
    into (a submodule, a nested repository) as one.

    Raises:
        WorktreeCopyError: When git cannot list it.
    """
    listed = subprocess.run(
        [
            "git",
            f"--git-dir={repository}",
            f"--work-tree={tree}",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=tree,
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        raise WorktreeCopyError(listed.stderr.decode(errors="replace").strip())
    return {
        os.fsdecode(each).rstrip("/") for each in listed.stdout.split(b"\0") if each
    }


def _walk(
    root: Path, excluded: frozenset[str], admitted: Set[str] | None
) -> Iterator[tuple[str, os.stat_result]]:
    """The directories and regular files under ``root`` by their relative
    paths, parents first, never following a link and never into ``.git``.

    With ``admitted``, only those paths, everything under the directories
    among them, and the directories on the way to them. They compare without
    case: where the host ignores it, git names a file as its index spells
    it, whatever the disk says.
    """
    folded = {
        each.casefold()
        for each in admitted or ()
        if _GIT not in PurePosixPath(each.casefold()).parts
    }
    on_the_way = {
        parent.as_posix() for each in folded for parent in PurePosixPath(each).parents
    }
    pending = [("", admitted is None)]
    while pending:
        directory, whole = pending.pop()
        with os.scandir(root / directory) as entries:
            for entry in sorted(entries, key=lambda each: each.name):
                path = f"{directory}/{entry.name}" if directory else entry.name
                if entry.name.casefold() == _GIT or path in excluded:
                    continue
                taken = whole or path.casefold() in folded
                if not taken and path.casefold() not in on_the_way:
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    yield path, info
                    pending.append((path, taken))
                elif stat.S_ISREG(info.st_mode) and taken:
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
            case ["changes", source, destination, *excluded]:
                listed: dict[str, CopiedFile] = {}
                for line in sys.stdin:
                    each: CopiedFile = json.loads(line)
                    listed[each["path"]] = each
                for changed in changed_files(
                    Path(source), Path(destination), listed, frozenset(excluded)
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
