"""Copy a working directory inside a command's microVM, and tell what changed.

A command that may write but is run in a directory no grant opens works on a
copy of it (``environment.spec.WorktreeCopy``): the host directory is
mounted read-only, and this module, run there (``python -m``), copies it onto
the microVM's own disk and later tells the host what the command changed.
The host never reads or writes the copy; it receives the changes as data and
writes them back itself, so nothing the command leaves there -- a hook, a
link, a configuration git would follow -- reaches the host.

Only directories and regular files are copied, and only regular files are
reported; ``.git`` is neither copied nor reported. Of a directory whose
``.git`` is a directory, what git ignores is not copied, and a new file is
not reported when git ignores it or it is in a repository of its own (a
submodule, a nested repository). Git is asked in both with the original's
``.git``, whatever the command did to the copy's, and names what it ignores
as it reads it from the disk. What git tracks is copied even when git here
takes it for an ignored one: the index of a repository made on macOS spells
it in another Unicode form than the disk. Any other directory is copied
whole. The copy gets a gitfile naming the read-only
original's ``.git``, so ``git status`` and ``git diff`` work while nothing
can be committed.

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
import unicodedata
from collections.abc import Iterator, Set
from pathlib import Path, PurePosixPath

from guildbotics.intelligences.agent_runtime.wire import (
    ChangedFile,
    CopiedFile,
)

_GIT = ".git"
_CHUNK_BYTES = 1 << 20
#: What git ignores: the untracked files its rules match.
_IGNORED = ("--others", "--ignored", "--exclude-standard")


class WorktreeCopyError(ValueError):
    """A copy whose changes cannot be told; the message says why."""


def copy_tree(
    source: Path, destination: Path, excluded: frozenset[str]
) -> Iterator[CopiedFile]:
    """Copy the directories and regular files of ``source`` to
    ``destination``, but what git ignores when ``source`` is a repository.

    Raises:
        WorktreeCopyError: When git cannot list the repository.
    """
    destination.mkdir(parents=True, exist_ok=True)
    repository = _repository(source)
    ignored = set() if repository is None else _ignored(repository, source)
    for path, info in _walk(source, excluded | ignored):
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

    In a repository, a new file is no change when git, listing the copy
    against ``source``'s repository, ignores it -- what the command built --
    or when it is in a directory that is a repository of its own in
    ``source`` (a submodule, a nested repository), where git does not look.
    An ignored directory holding copied files is looked into for them alone.
    A link or anything else the command made is no file to write back and is
    left out; a copied file that became one cannot be told.

    Raises:
        WorktreeCopyError: For a copied file that is no regular file anymore,
            or when git cannot list the copy.
    """
    repository = _repository(source)
    kept = copied.keys() | {
        parent.as_posix() for each in copied for parent in PurePosixPath(each).parents
    }
    ignored = set() if repository is None else _ignored(repository, destination)
    # The directories whose new files are no change.
    untold = ignored & kept
    present: set[str] = set()
    for path, info in _walk(destination, excluded | (ignored - kept)):
        if stat.S_ISDIR(info.st_mode):
            if repository is not None and os.path.lexists(source / path / _GIT):
                untold.add(path)
            continue
        present.add(path)
        before = copied.get(path)
        if before is None and not untold.isdisjoint(
            parent.as_posix() for parent in PurePosixPath(path).parents
        ):
            continue
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


def _ignored(repository: Path, tree: Path) -> set[str]:
    """What git ignores in ``tree`` with ``repository``: the untracked files
    its rules match, a directory holding nothing else as one. The names are
    the ones git read from ``tree``: in the microVM, where git changes no
    Unicode form, spelled as the disk spells them.

    Git on macOS composes the names it adds to the index, and git here does
    not: it takes the disk's other form of a tracked file for an untracked
    one, which an ignore rule may match, and a directory holding it for one
    holding nothing tracked. Such a file is no ignored one, and what is
    ignored in such a directory is told file by file.

    Raises:
        WorktreeCopyError: When git cannot list it.
    """
    tracked = {_composed(each) for each in _listed(repository, tree, "--cached")}
    holding = {
        str(parent) for each in tracked for parent in PurePosixPath(each).parents
    }
    ignored: set[str] = set()
    for each in _listed(repository, tree, *_IGNORED, "--directory"):
        if _composed(each) in holding:
            ignored |= _listed(repository, tree, *_IGNORED, "--", f"{each}/")
        else:
            ignored.add(each)
    return {each for each in ignored if _composed(each) not in tracked}


def _listed(repository: Path, tree: Path, *selection: str) -> set[str]:
    """What ``git ls-files`` names in ``tree`` with ``repository``.

    Raises:
        WorktreeCopyError: When git cannot list it.
    """
    listed = subprocess.run(
        [
            "git",
            # A directory's name is no pathspec magic, as ``:name`` would be.
            "--literal-pathspecs",
            f"--git-dir={repository}",
            f"--work-tree={tree}",
            "ls-files",
            "-z",
            *selection,
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


def _composed(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def _walk(root: Path, left_out: Set[str]) -> Iterator[tuple[str, os.stat_result]]:
    """The directories and regular files under ``root`` by their relative
    paths, parents first, never following a link and never into ``.git`` or
    what is ``left_out``."""
    pending = [""]
    while pending:
        directory = pending.pop()
        with os.scandir(root / directory) as entries:
            for entry in sorted(entries, key=lambda each: each.name):
                path = f"{directory}/{entry.name}" if directory else entry.name
                if entry.name.casefold() == _GIT or path in left_out:
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
