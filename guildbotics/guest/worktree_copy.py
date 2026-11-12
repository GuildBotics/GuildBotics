"""Copy a working directory inside a command's microVM, and tell what changed.

A command that may write but is run in a directory no grant opens works on a
copy of it (``environment.spec.WorktreeCopy``): the host directory is
mounted read-only, and this module, run there (``python -m``), copies it onto
the microVM's own disk and later tells the host what the command changed.
The host never reads or writes the copy; it receives the changes as data and
writes them back itself, so nothing the command leaves there -- a hook, a
link, a configuration git would follow -- reaches the host.

The host selects directories and regular files with ``gb_base::worktree``,
using the original repository and isolated host Git configuration. ``copy``
reads that manifest from standard input and materializes it without querying
Git. The copy gets a gitfile naming the read-only original's ``.git`` when
its root has a repository directory.

Change detection still asks the microVM's Git against the original's ``.git``
and preserves its Unicode handling. It neither trusts the copy's gitfile
nor reports new files inside submodules or nested repositories.

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
import tempfile
import unicodedata
from collections.abc import Iterator, Set
from pathlib import Path, PurePosixPath
from typing import IO, Any, TypedDict

from guildbotics.intelligences.agent_runtime.wire import (
    ChangedFile,
    CopiedFile,
)

_GIT = ".git"
_CHUNK_BYTES = 1 << 20
#: What git ignores: the untracked files its rules match.
_IGNORED = ("--others", "--ignored", "--exclude-standard")


class CopyManifest(TypedDict):
    """The host's selection, with on-disk spelling."""

    directories: list[str]
    files: list[str]
    repository: bool


class WorktreeCopyError(ValueError):
    """A copy whose changes cannot be told; the message says why."""


def receive_tree(
    source: Path, destination: Path, upload: dict[str, Any], *, readonly: bool = True
) -> Iterator[CopiedFile]:
    """Rebuild a client copy using only received data, never the client's path.

    A file the client named by its index blob is written from the received
    history; any other file from the bytes it sent.
    """

    def safe(name: str) -> Path:
        parts = name.split("/")
        if (
            not name
            or name.startswith("/")
            or parts[0] == ".guildbotics"
            or "\\" in name
            or any(
                part in ("", ".", "..") or part.casefold() == ".git" for part in parts
            )
        ):
            raise WorktreeCopyError("Unsafe received worktree path")
        return Path(name)

    destination.mkdir(parents=True, exist_ok=True)
    source.mkdir(parents=True, exist_ok=True)
    for name in upload["directories"]:
        (destination / safe(name)).mkdir(parents=True, exist_ok=True)
    for name in upload["nested"]:
        (source / safe(name) / _GIT).mkdir(parents=True, exist_ok=True)
    repository = upload["git"]
    gitdir = source / _GIT
    if repository is not None:
        _receive_repository(gitdir, destination, repository)
    received: list[tuple[dict[str, Any], Path]] = []
    named: dict[str, list[Path]] = {}
    for item in upload["files"]:
        target = destination / safe(item["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        match item["source"]:
            case {"content": content}:
                with target.open("xb") as file:
                    file.write(base64.b64decode(content, validate=True))
            case {"blob": {"oid": str(oid)}}:
                named.setdefault(oid, []).append(target)
            case _:
                raise WorktreeCopyError("Invalid received worktree file")
        received.append((item, target))
    _write_blobs(gitdir, named)
    for item, target in received:
        os.chmod(target, 0o755 if item["executable"] else 0o644)
        yield CopiedFile(
            path=item["path"], sha256=_sha256(target), executable=item["executable"]
        )
    if repository is not None and readonly:
        subprocess.run(["mount", "--bind", str(gitdir), str(gitdir)], check=True)
        subprocess.run(["mount", "-o", "remount,bind,ro", str(gitdir)], check=True)


def _receive_repository(
    gitdir: Path, destination: Path, repository: dict[str, Any]
) -> None:
    """Rebuild the client's repository in ``gitdir`` for ``destination``."""
    object_format = repository["object_format"]
    if object_format not in ("sha1", "sha256"):
        raise WorktreeCopyError("Invalid Git object format")
    subprocess.run(
        ["git", "init", "--bare", f"--object-format={object_format}", str(gitdir)],
        check=True,
        capture_output=True,
    )

    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", f"--git-dir={gitdir}", *args], check=True, capture_output=True
        ).stdout

    if repository["bundle"]:
        bundle = gitdir.parent / "history.bundle"
        bundle.write_bytes(base64.b64decode(repository["bundle"], validate=True))
        git("bundle", "verify", str(bundle))
        git("bundle", "unbundle", str(bundle))
        bundle.unlink()
    for expected, content in repository["objects"].items():
        actual = (
            subprocess.run(
                ["git", f"--git-dir={gitdir}", "hash-object", "-w", "--stdin"],
                input=base64.b64decode(content, validate=True),
                check=True,
                capture_output=True,
            )
            .stdout.decode()
            .strip()
        )
        if actual != expected:
            raise WorktreeCopyError("Received index object hash mismatch")
    for name, commit in repository["refs"].items():
        if not name.startswith(("refs/heads/", "refs/remotes/", "refs/tags/")):
            raise WorktreeCopyError("Invalid received Git ref")
        git("update-ref", name, commit)
    head = repository["head"]
    if head.startswith("refs/heads/"):
        git("symbolic-ref", "HEAD", head)
    else:
        git("update-ref", "--no-deref", "HEAD", head)
    for name, content in (
        ("index", repository["index"]),
        ("info/exclude", repository["exclude"]),
    ):
        if content is not None:
            (gitdir / name).write_bytes(base64.b64decode(content, validate=True))
    for name, content in repository["shared_indexes"].items():
        if not name.startswith("sharedindex.") or any(
            c not in "0123456789abcdef" for c in name[12:]
        ):
            raise WorktreeCopyError("Invalid shared index name")
        (gitdir / name).write_bytes(base64.b64decode(content, validate=True))
    git("config", "core.bare", "false")
    git("config", "core.worktree", str(destination))
    for name, url in repository["remotes"].items():
        git("config", f"remote.{name}.url", url)
    (destination / _GIT).write_text(f"gitdir: {gitdir}\n", encoding="utf-8")


def _write_blobs(gitdir: Path, named: dict[str, list[Path]]) -> None:
    """Write each blob of the received repository to the files that name it,
    streamed from one ``git cat-file --batch``.

    Raises:
        WorktreeCopyError: When the repository lacks one of them.
    """
    if not named:
        return
    with tempfile.TemporaryFile() as wanted:
        wanted.write("".join(f"{oid}\n" for oid in named).encode())
        wanted.seek(0)
        with subprocess.Popen(
            ["git", f"--git-dir={gitdir}", "cat-file", "--batch"],
            stdin=wanted,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as batch:
            assert batch.stdout is not None
            for oid, targets in named.items():
                _copy_blob(batch.stdout, oid, targets)
    if batch.returncode != 0:
        raise WorktreeCopyError("Received history lacks a worktree blob")


def _copy_blob(out: IO[bytes], oid: str, targets: list[Path]) -> None:
    """Stream one batch entry into the first target, then copy it to the
    rest one at a time: a blob many files share opens no more of them."""
    match out.readline().decode().split():
        case [found, "blob", size] if found == oid:
            remaining = int(size)
        case _:
            raise WorktreeCopyError("Received history lacks a worktree blob")
    first, *rest = targets
    with first.open("xb") as file:
        while remaining:
            chunk = out.read(min(remaining, _CHUNK_BYTES))
            if not chunk:
                raise WorktreeCopyError("Received history lacks a worktree blob")
            file.write(chunk)
            remaining -= len(chunk)
    if out.read(1) != b"\n":
        raise WorktreeCopyError("Received history lacks a worktree blob")
    for target in rest:
        with first.open("rb") as source, target.open("xb") as copy:
            shutil.copyfileobj(source, copy, _CHUNK_BYTES)


def copy_tree(
    source: Path, destination: Path, manifest: CopyManifest
) -> Iterator[CopiedFile]:
    """Materialize exactly the host-selected directories and regular files."""
    destination.mkdir(parents=True, exist_ok=True)
    for path in manifest["directories"]:
        (destination / path).mkdir(exist_ok=True)
    for path in manifest["files"]:
        info = (source / path).lstat()
        if not stat.S_ISREG(info.st_mode):
            raise WorktreeCopyError(f"'{path}' is no longer a regular file.")
        target = destination / path
        shutil.copyfile(source / path, target, follow_symlinks=False)
        executable = _executable(info)
        os.chmod(target, 0o755 if executable else 0o644)
        yield CopiedFile(path=path, sha256=_sha256(target), executable=executable)
    if manifest["repository"]:
        (destination / _GIT).write_text(f"gitdir: {source / _GIT}\n", encoding="utf-8")


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
            case ["receive", source, destination, *excluded]:
                for copied in receive_tree(
                    Path(source), Path(destination), json.load(sys.stdin)
                ):
                    out.write(json.dumps(copied) + "\n")
            case ["copy", source, destination, *excluded]:
                for copied in copy_tree(
                    Path(source), Path(destination), json.load(sys.stdin)
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
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(exc, file=sys.stderr)
        return 1
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
