"""The copy a command works on, as its microVM makes it and tells its changes."""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path

import pytest

from guildbotics.runtime import worktree_copy
from guildbotics.runtime.worktree_copy import (
    WorktreeCopyError,
    changed_files,
    copy_tree,
)


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content, encoding="utf-8")
    return root


def _copied(source: Path, destination: Path, excluded=frozenset()):
    return {each["path"]: each for each in copy_tree(source, destination, excluded)}


def test_the_copy_holds_the_files_and_directories_but_not_git(tmp_path: Path) -> None:
    """Nothing of ``.git`` is copied, under any spelling; a gitfile names the
    read-only original's instead, so git reads it and can change nothing."""
    source = _tree(
        tmp_path / "source",
        {"a.txt": "a", "src/b.py": "b", ".git/config": "x", "sub/.GIT/hooks/h": "x"},
    )
    (source / "empty").mkdir()
    destination = tmp_path / "copy"

    copied = _copied(source, destination)

    assert sorted(copied) == ["a.txt", "src/b.py"]
    assert (destination / "src" / "b.py").read_text(encoding="utf-8") == "b"
    assert (destination / "empty").is_dir()
    assert not (destination / "sub" / ".GIT").exists()
    assert (destination / ".git").read_text(encoding="utf-8") == (
        f"gitdir: {source / '.git'}\n"
    )


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

    changes = list(changed_files(destination, copied, frozenset()))

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

    [change] = changed_files(destination, copied, frozenset())

    assert change["path"] == "run.sh"
    assert change["executable"] is True


def test_a_link_the_command_made_is_no_change_but_a_file_that_became_one_fails(
    tmp_path: Path, symlinks: None
) -> None:
    source = _tree(tmp_path / "source", {"a.txt": "a"})
    destination = tmp_path / "copy"
    copied = _copied(source, destination)
    (destination / "made").symlink_to(tmp_path)

    assert list(changed_files(destination, copied, frozenset())) == []

    (destination / "a.txt").unlink()
    (destination / "a.txt").symlink_to(tmp_path)
    with pytest.raises(WorktreeCopyError, match="no longer a regular file"):
        list(changed_files(destination, copied, frozenset()))


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
    assert worktree_copy.main(["changes", str(destination)]) == 0
    [change] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert change["path"] == "a.txt"

    assert worktree_copy.main(["unknown"]) == 1
    assert "Unknown arguments" in capsys.readouterr().err
