"""The copy a command works on, as its microVM makes it and tells its changes."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from guildbotics.guest import worktree_copy
from guildbotics.guest.worktree_copy import (
    WorktreeCopyError,
    changed_files,
    copy_tree,
)


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content, encoding="utf-8")
    return root


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repository(root: Path, tracked: dict[str, str]) -> Path:
    """A repository whose index holds ``tracked``; nothing needs committing."""
    _tree(root, tracked)
    _git(root, "init", "-q")
    _git(root, "add", "--", *tracked)
    return root


def _copied(source: Path, destination: Path, excluded=frozenset()):
    return {each["path"]: each for each in copy_tree(source, destination, excluded)}


def _changes(source: Path, destination: Path, copied, excluded=frozenset()):
    return list(changed_files(source, destination, copied, excluded))


def test_a_directory_without_git_is_copied_whole(tmp_path: Path) -> None:
    """Without a repository nothing tells what is ignored: a ``.gitignore``
    is a file like any other, and an empty directory is copied too."""
    source = _tree(
        tmp_path / "source",
        {".gitignore": "target/\n", "a.txt": "a", "target/big": "b", "x/.GIT/h": "x"},
    )
    (source / "empty").mkdir()
    destination = tmp_path / "copy"

    copied = _copied(source, destination)

    assert sorted(copied) == [".gitignore", "a.txt", "target/big"]
    assert (destination / "empty").is_dir()
    assert not (destination / "x" / ".GIT").exists()
    assert not (destination / ".git").exists()
    _tree(destination, {"target/out": "o"})
    assert [each["path"] for each in _changes(source, destination, copied)] == [
        "target/out"
    ]


def test_a_repository_is_copied_as_far_as_git_names_it(tmp_path: Path) -> None:
    """What git tracks and the untracked files it does not ignore; never
    ``.git``, under any spelling, nor a tracked file gone from the worktree.
    A gitfile names the read-only original's ``.git`` instead, so git reads
    it and can change nothing."""
    source = _repository(
        tmp_path / "source",
        {".gitignore": "target/\n*.pyc\n", "a.txt": "a", "gone.txt": "g"},
    )
    (source / "gone.txt").unlink()
    _tree(
        source,
        {
            "src/b.py": "b",
            "src/b.pyc": "c",
            "target/big": "t",
            "sub/.GIT/hooks/h": "x",
        },
    )
    (source / "empty").mkdir()
    destination = tmp_path / "copy"

    copied = _copied(source, destination)

    assert sorted(copied) == [".gitignore", "a.txt", "src/b.py"]
    assert (destination / "src" / "b.py").read_text(encoding="utf-8") == "b"
    assert not (destination / "target").exists()
    assert not (destination / "empty").exists()
    assert not (destination / "sub").exists()
    assert (destination / ".git").read_text(encoding="utf-8") == (
        f"gitdir: {source / '.git'}\n"
    )


def test_a_name_the_disk_spells_in_another_case_is_copied(tmp_path: Path) -> None:
    """Where the host ignores case, git names a tracked file as its index
    spells it, and a renamed directory with it."""
    source = _repository(tmp_path / "source", {"README.md": "r", "Src/b.py": "b"})
    _git(source, "config", "core.ignorecase", "true")
    for old, new in (("README.md", "Readme.md"), ("Src", "src")):
        (source / old).rename(source / f"{new}.tmp")
        (source / f"{new}.tmp").rename(source / new)

    assert sorted(_copied(source, tmp_path / "copy")) == ["Readme.md", "src/b.py"]


def test_what_the_command_built_is_no_change(tmp_path: Path) -> None:
    """A new file git ignores is not told, whatever the command did to the
    gitfile: the listing reads the original's ``.git``."""
    source = _repository(
        tmp_path / "source",
        {".gitignore": "target/\n__pycache__/\n", "a.txt": "a", "gone.txt": "g"},
    )
    destination = tmp_path / "copy"
    copied = _copied(source, destination)
    (destination / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    (destination / "a.txt").write_text("edited", encoding="utf-8")
    (destination / "gone.txt").unlink()
    _tree(destination, {"target/out": "o", "__pycache__/x.pyc": "c", "new.py": "n"})

    changes = _changes(source, destination, copied)

    assert [each["path"] for each in changes] == ["a.txt", "new.py", "gone.txt"]


def test_a_submodule_and_a_nested_repository_are_copied_whole(
    tmp_path: Path,
) -> None:
    """Git does not look into them, so neither does the listing; their
    ``.git`` stays behind. In the copy they are no repository, so a new file
    there counts as git names it from the outer repository."""
    source = _repository(tmp_path / "source", {".gitignore": "*.log\n"})
    _repository(source / "nested", {"n.txt": "n", "old.log": "o"})
    _tree(source, {"sub/s.txt": "s", "sub/.git": "gitdir: ../.git/modules/sub\n"})
    blob = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    _git(source, "update-index", "--add", "--cacheinfo", f"160000,{blob},sub")
    destination = tmp_path / "copy"

    copied = _copied(source, destination)

    assert sorted(copied) == [
        ".gitignore",
        "nested/n.txt",
        "nested/old.log",
        "sub/s.txt",
    ]
    assert not os.path.lexists(destination / "nested" / ".git")
    assert not os.path.lexists(destination / "sub" / ".git")
    (destination / "nested" / "old.log").write_text("edited", encoding="utf-8")
    _tree(destination, {"nested/new.log": "x", "nested/new.txt": "x"})

    changes = _changes(source, destination, copied)

    assert [each["path"] for each in changes] == ["nested/new.txt", "nested/old.log"]


def test_a_gitfile_names_no_repository_here_so_all_is_copied(
    tmp_path: Path,
) -> None:
    """A linked worktree's ``.git`` names a directory elsewhere on the host,
    which the microVM does not hold: it is copied whole, with no gitfile."""
    source = _tree(
        tmp_path / "source",
        {".git": "gitdir: /elsewhere/.git/worktrees/w\n", ".gitignore": "t/\n"},
    )
    _tree(source, {"t/big": "b"})
    destination = tmp_path / "copy"

    assert sorted(_copied(source, destination)) == [".gitignore", "t/big"]
    assert not (destination / ".git").exists()


def test_a_repository_git_cannot_read_fails(tmp_path: Path) -> None:
    source = _tree(tmp_path / "source", {".git/config": "x", "a.txt": "a"})

    with pytest.raises(WorktreeCopyError, match="git"):
        _copied(source, tmp_path / "copy")


def test_a_link_and_an_excluded_directory_are_not_copied(
    tmp_path: Path, symlinks: None
) -> None:
    source = _tree(tmp_path / "source", {"a.txt": "a", "docs/shared/s.md": "s"})
    (source / "link").symlink_to(tmp_path)
    (source / "file-link").symlink_to(source / "a.txt")

    copied = _copied(source, tmp_path / "copy", frozenset({"docs/shared"}))

    assert sorted(copied) == ["a.txt"]
    assert not os.path.lexists(tmp_path / "copy" / "link")
    assert not (tmp_path / "copy" / "docs" / "shared").exists()


def test_changes_are_what_differs_from_the_copy(tmp_path: Path) -> None:
    source = _tree(
        tmp_path / "source", {"same.txt": "s", "edit.txt": "e", "gone.txt": "g"}
    )
    destination = tmp_path / "copy"
    copied = _copied(source, destination)
    (destination / "edit.txt").write_text("edited", encoding="utf-8")
    (destination / "gone.txt").unlink()
    _tree(destination, {"new/n.bin": "n"})

    changes = _changes(source, destination, copied)

    assert changes == [
        # The bit is the host's to keep unless the command changed it.
        {"path": "edit.txt", "content": base64.b64encode(b"edited").decode()},
        {
            "path": "new/n.bin",
            "executable": False,
            "content": base64.b64encode(b"n").decode(),
        },
        {"path": "gone.txt", "deleted": True},
    ]


def test_an_executable_bit_alone_is_a_change(
    tmp_path: Path, posix_permissions: None
) -> None:
    source = _tree(tmp_path / "source", {"run.sh": "#!/bin/sh\n"})
    destination = tmp_path / "copy"
    copied = _copied(source, destination)
    os.chmod(destination / "run.sh", 0o755)

    [change] = _changes(source, destination, copied)

    assert change["path"] == "run.sh"
    assert change["executable"] is True


def test_a_link_the_command_made_is_no_change_but_a_file_that_became_one_fails(
    tmp_path: Path, symlinks: None
) -> None:
    source = _tree(tmp_path / "source", {"a.txt": "a"})
    destination = tmp_path / "copy"
    copied = _copied(source, destination)
    (destination / "made").symlink_to(tmp_path)

    assert _changes(source, destination, copied) == []

    (destination / "a.txt").unlink()
    (destination / "a.txt").symlink_to(tmp_path)
    with pytest.raises(WorktreeCopyError, match="no longer a regular file"):
        _changes(source, destination, copied)


def test_the_entry_copies_and_tells_changes_as_json_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _tree(tmp_path / "source", {"a.txt": "a"})
    destination = tmp_path / "copy"

    assert worktree_copy.main(["copy", str(source), str(destination)]) == 0
    listed = capsys.readouterr().out
    assert [json.loads(line)["path"] for line in listed.splitlines()] == ["a.txt"]

    (destination / "a.txt").write_text("b", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(listed))
    assert worktree_copy.main(["changes", str(source), str(destination)]) == 0
    [change] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert change["path"] == "a.txt"

    assert worktree_copy.main(["unknown"]) == 1
    assert "Unknown arguments" in capsys.readouterr().err
