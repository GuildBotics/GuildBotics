"""The real Rust client export and guest reconstruction share one data contract."""

import base64
import json
import subprocess
from pathlib import Path

import pytest

from guildbotics.guest.worktree_copy import changed_files, receive_tree


def git(root, *args, input=None):
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            *args,
        ],
        cwd=root,
        input=input,
        check=True,
        capture_output=True,
    ).stdout


@pytest.fixture(scope="module")
def exporter():
    executable = Path("target/debug/examples/worktree-transfer").resolve()
    assert executable.is_file(), (
        "Build gb-environment --example worktree-transfer before running this contract test"
    )
    return executable


@pytest.mark.parametrize("detached", [False, True])
def test_export_rebuild_and_changes_without_original_path(tmp_path, exporter, detached):
    original = tmp_path / "original"
    original.mkdir()
    git(original, "init", "-b", "main")
    for name, content in {
        "file": "committed",
        "deleted": "before",
        ".gitignore": "ignored\n",
    }.items():
        (original / name).write_text(content)
    git(original, "add", ".")
    git(original, "commit", "-m", "fixture")
    git(original, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(original, "tag", "fixture")
    git(
        original,
        "remote",
        "add",
        "origin",
        "https://fixture:credential@example.test/repo.git",
    )
    git(original, "remote", "add", "local", "/fixture/path@work")
    (original / ".git/info/exclude").write_text("excluded\n")
    (original / "file").write_text("staged")
    git(original, "add", "file")
    (original / "file").write_text("unstaged")
    (original / "deleted").unlink()
    (original / "new").write_text("new before run")
    (original / "ignored").write_text("left out")
    (original / ".guildbotics").mkdir()
    (original / ".guildbotics/private").write_text("not transferred")
    nested = original / "nested"
    nested.mkdir()
    git(nested, "init")
    (nested / "before").write_text("old")
    submodule = original / "submodule"
    submodule.mkdir()
    git(submodule, "init")
    (submodule / "inside").write_text("old")
    git(submodule, "add", ".")
    git(submodule, "commit", "-m", "submodule fixture")
    git(original, "add", "submodule")
    if detached:
        git(original, "checkout", "--detach")
        git(original, "update-index", "--split-index")
    before_status = (
        b"\n".join(
            line
            for line in git(original, "status", "--porcelain").splitlines()
            if b".guildbotics/" not in line
        )
        + b"\n"
    )
    before_diff = git(original, "diff")
    upload = json.loads(subprocess.check_output([str(exporter), str(original)]))
    assert all(not f["path"].startswith(".guildbotics") for f in upload["files"])
    assert upload["git"]["remotes"] == {
        "origin": "https://example.test/repo.git",
        "local": "/fixture/path@work",
    }
    # Make the original unreachable before the guest even starts reconstruction.
    original.rename(tmp_path / "removed")
    source, destination = tmp_path / "source", tmp_path / "guest"
    copied = {
        c["path"]: c for c in receive_tree(source, destination, upload, readonly=False)
    }
    assert git(destination, "status", "--porcelain") == before_status
    assert git(destination, "diff") == before_diff
    assert b"fixture" in git(destination, "log", "origin/main", "--oneline")
    assert b"https://example.test/repo.git" in git(destination, "remote", "-v")
    (destination / "file").write_text("result")
    (destination / "new").unlink()
    (destination / "added").write_text("created")
    (destination / "ignored").write_text("not returned")
    (destination / "excluded").write_text("not returned")
    (destination / "nested/new").write_text("not returned")
    (destination / "submodule/new").write_text("not returned")
    changes = {
        c["path"]: c
        for c in changed_files(source, destination, copied, frozenset({".guildbotics"}))
    }
    assert set(changes) == {"file", "new", "added"}
    assert changes["new"]["deleted"]
    assert base64.b64decode(changes["file"]["content"]) == b"result"


def test_readonly_git_is_mounted_before_command(tmp_path, monkeypatch):
    calls = []
    real_run = subprocess.run

    def run(args, **kwargs):
        if args[0] == "mount":
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    upload = {
        "directories": [],
        "files": [],
        "nested": [],
        "git": {
            "bundle": "",
            "object_format": "sha1",
            "head": "refs/heads/main",
            "refs": {},
            "index": None,
            "exclude": None,
            "remotes": {},
            "objects": {},
            "shared_indexes": {},
        },
    }
    list(receive_tree(tmp_path / "source", tmp_path / "guest", upload))
    assert calls[0][1] == "--bind"
    assert calls[1][1:3] == ["-o", "remount,bind,ro"]


@pytest.mark.parametrize("state", ["clean", "staged", "unborn", "conflict"])
def test_only_index_blobs_absent_from_exported_head_are_sent(tmp_path, exporter, state):
    import random

    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    # Incompressible data pins the regression: files + bundle fit, three copies do not.
    (root / "file").write_bytes(random.Random(42).randbytes(6 * 1024 * 1024))
    git(root, "add", ".")
    expected = set()
    original = git(root, "rev-parse", ":file").strip().decode()
    if state != "unborn":
        git(root, "commit", "-m", "fixture")
    else:
        expected.add(original)
    if state == "staged":
        (root / "file").write_bytes(b"staged\0content")
        git(root, "add", ".")
        expected.add(git(root, "rev-parse", ":file").strip().decode())
    if state == "conflict":
        other = (
            git(root, "hash-object", "-w", "--stdin", input=b"other\0content")
            .strip()
            .decode()
        )
        git(
            root,
            "update-index",
            "--index-info",
            input=(
                f"0 {'0' * 40}\tfile\n100644 {original} 1\tfile\n100644 {other} 2\tfile\n100644 {other} 3\tfile\n"
            ).encode(),
        )
        expected.add(other)
    upload = json.loads(subprocess.check_output([str(exporter), str(root)]))
    assert set(upload["git"]["objects"]) == expected
    root.rename(tmp_path / "hidden")
    source, destination = tmp_path / "source", tmp_path / "destination"
    list(receive_tree(source, destination, upload, readonly=False))
    git(destination, "diff", "--cached")


def test_many_index_objects_are_batched_without_pipe_deadlock(tmp_path, exporter):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    for i in range(1800):
        (root / str(i)).write_bytes(str(i).encode() + b"x" * 100)
    git(root, "add", ".")
    upload = json.loads(subprocess.check_output([str(exporter), str(root)], timeout=30))
    assert len(upload["git"]["objects"]) == 1800
