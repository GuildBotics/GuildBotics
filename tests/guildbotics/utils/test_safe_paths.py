from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.utils import safe_paths
from guildbotics.utils.safe_paths import (
    HostPathPermissionError,
    UnsafePathError,
    host_path_contains,
    inspect_host_path,
    open_host_file,
    read_host_file,
)


def test_missing_directories_are_created_below_checked_parents(tmp_path: Path) -> None:
    target = tmp_path / "new" / "child"
    before = inspect_host_path(target, missing=True)
    assert not before.present
    assert before.missing == ("new", "child")
    after = inspect_host_path(target, create=True)
    assert after.present and target.is_dir()
    assert inspect_host_path(tmp_path).contains(after)
    assert not after.contains(inspect_host_path(tmp_path))


@pytest.mark.parametrize("leaf", [False, True])
@pytest.mark.parametrize("create", [False, True])
def test_links_are_refused_before_reading_or_creating(
    tmp_path: Path, symlinks, leaf: bool, create: bool
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    target = link if leaf else link / "new"
    with pytest.raises(UnsafePathError):
        inspect_host_path(target, create=create, missing=True)
    assert list(outside.iterdir()) == []


def test_regular_file_is_read_through_the_opened_leaf(tmp_path: Path) -> None:
    file = tmp_path / "account.json"
    file.write_bytes(b"account")
    assert read_host_file(file) == b"account"
    assert inspect_host_path(file, directory=False).present
    with pytest.raises(UnsafePathError):
        inspect_host_path(file)


def test_host_file_creation_is_regular_and_never_follows_a_leaf_link(
    tmp_path, symlinks
):
    leaf = tmp_path / "lock"
    with open_host_file(leaf) as file:
        file.write("saved")
    assert leaf.read_text() == "saved"
    target = tmp_path / "outside"
    target.write_text("private")
    leaf.unlink()
    leaf.symlink_to(target)
    with pytest.raises((UnsafePathError, OSError)):
        open_host_file(leaf)
    assert target.read_text() == "private"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS fixed aliases")
def test_fixed_os_alias_is_normalized_before_inspection() -> None:
    assert inspect_host_path(Path("/tmp")).path == Path("/private/tmp")
    assert inspect_host_path(Path("/var")).path == Path("/private/var")


def test_missing_names_use_the_ancestor_filesystem_case_rules(tmp_path: Path) -> None:
    first = inspect_host_path(tmp_path / "private", missing=True)
    second = inspect_host_path(tmp_path / "PRIVATE/child", missing=True)
    assert first.contains(second) is not first.case_sensitive


def test_absent_equivalent_unicode_names_on_insensitive_filesystems(tmp_path):
    first = inspect_host_path(tmp_path / "caf\u00e9", missing=True)
    second = inspect_host_path(tmp_path / "cafe\u0301/child", missing=True)
    assert first.contains(second) is not first.case_sensitive


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX directory-relative permission failure"
)
@pytest.mark.parametrize("operation", ["inspect", "read", "open", "visit", "contains"])
def test_public_path_operations_preserve_absolute_permission_path(
    tmp_path, monkeypatch, operation
):
    path = tmp_path / "private"
    path.mkdir()
    original = os.open

    def deny(name, *args, **kwargs):
        if name == "private":
            raise PermissionError(13, "denied", "private")
        return original(name, *args, **kwargs)

    target = inspect_host_path(path)
    monkeypatch.setattr(os, "open", deny)
    actions = {
        "inspect": lambda: inspect_host_path(path),
        "read": lambda: read_host_file(path),
        "open": lambda: open_host_file(path / "file"),
        "visit": lambda: safe_paths.visit_host_directory(path, lambda fd: None),
        "contains": lambda: host_path_contains(path, target),
    }
    with pytest.raises(HostPathPermissionError) as error:
        actions[operation]()
    assert Path(error.value.filename).is_absolute()
    assert Path(error.value.filename).is_relative_to(path)


def test_ordinary_rename_does_not_change_the_object_relationship(
    tmp_path: Path,
) -> None:
    root = tmp_path / "before"
    (root / "child").mkdir(parents=True)
    parent = inspect_host_path(root)
    after = tmp_path / "after"
    root.rename(after)
    assert parent.contains(inspect_host_path(after / "child"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX unrelated-folder permission check")
def test_workspace_ancestry_does_not_open_an_unrelated_grant(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    documents = tmp_path / "Documents"
    documents.mkdir()
    target = inspect_host_path(workspace)
    original = os.open

    def deny_documents(path, *args, **kwargs):
        if path == "Documents":
            raise PermissionError("private folder")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_documents)
    assert not host_path_contains(documents / "exchange", target)
    with pytest.raises(HostPathPermissionError):
        inspect_host_path(documents / "exchange", missing=True)


@pytest.mark.parametrize("present", [False, True])
def test_workspace_ancestry_still_refuses_a_granted_parent(tmp_path, present):
    parent = tmp_path / "shared"
    target = parent / "workspace"
    if present:
        target.mkdir(parents=True)
    assert host_path_contains(parent, inspect_host_path(target, missing=True))


def test_existing_case_aliases_have_the_same_identity(tmp_path):
    root = tmp_path / "MixedCase"
    (root / "child").mkdir(parents=True)
    alias = tmp_path / "mixedcase"
    if not alias.exists():
        pytest.skip("case-sensitive filesystem")
    assert inspect_host_path(root).contains(inspect_host_path(alias / "child"))


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Data volume firmlink")
def test_firmlink_aliases_have_the_same_ancestry(tmp_path):
    (tmp_path / "child").mkdir()
    alias = Path("/System/Volumes/Data") / tmp_path.relative_to("/")
    if not alias.exists():
        pytest.skip("no Data volume alias on this filesystem")
    assert inspect_host_path(tmp_path).contains(inspect_host_path(alias / "child"))


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions")
@pytest.mark.parametrize("create", [False, True])
def test_windows_junction_is_refused_before_creating_children(
    tmp_path: Path, create: bool
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(UnsafePathError):
        inspect_host_path(link / "new", create=create, missing=True)
    assert not (outside / "new").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fstat identity")
def test_unknown_object_identity_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(safe_paths.os, "fstat", lambda fd: SimpleNamespace(st_ino=0))
    with pytest.raises(UnsafePathError):
        inspect_host_path(tmp_path)


def test_missing_siblings_are_compared_below_the_opened_ancestor(
    tmp_path: Path,
) -> None:
    parent = inspect_host_path(tmp_path / "missing", missing=True)
    assert parent.contains(
        inspect_host_path(tmp_path / "missing" / "child", missing=True)
    )
    assert not parent.contains(inspect_host_path(tmp_path / "other", missing=True))


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-relative creation")
def test_creation_keeps_the_opened_parent_when_its_name_is_swapped(
    tmp_path: Path, monkeypatch, symlinks
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original = os.mkdir
    swapped = False

    def mkdir(name, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if name == "new" and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(tmp_path / "original")
            parent.symlink_to(outside, target_is_directory=True)
        return original(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", mkdir)
    inspect_host_path(parent / "new", create=True)
    assert (tmp_path / "original/new").is_dir()
    assert not (outside / "new").exists()
    with pytest.raises(UnsafePathError):
        inspect_host_path(parent / "new")
