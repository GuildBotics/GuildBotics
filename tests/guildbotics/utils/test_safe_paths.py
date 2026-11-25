from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.utils import safe_paths
from guildbotics.utils.safe_paths import (
    HostPathPermissionError,
    UnsafePathError,
    inspect_host_path,
)


def test_missing_directories_are_created_below_checked_parents(tmp_path: Path) -> None:
    target = tmp_path / "new" / "child"
    before = inspect_host_path(target, missing=True)
    assert not before.present
    assert before.missing == ("new", "child")
    after = inspect_host_path(target, create=True)
    assert after.present and target.is_dir()
    assert after.identities[:-2] == inspect_host_path(tmp_path).identities


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


def test_regular_file_requires_file_inspection(tmp_path: Path) -> None:
    file = tmp_path / "account.json"
    file.write_bytes(b"account")
    assert inspect_host_path(file, directory=False).present
    with pytest.raises(UnsafePathError):
        inspect_host_path(file)


def test_inspection_preserves_absolute_permission_path(tmp_path, monkeypatch):
    path = tmp_path / "blocked"
    path.mkdir()
    original = os.open

    def deny(name, *args, **kwargs):
        if name == "blocked":
            raise PermissionError(13, "denied", "blocked")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny)
    with pytest.raises(HostPathPermissionError) as error:
        inspect_host_path(path)
    assert Path(error.value.filename).is_absolute()
    assert Path(error.value.filename) == path


def test_unknown_object_identity_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(safe_paths.os, "fstat", lambda fd: SimpleNamespace(st_ino=0))
    with pytest.raises(UnsafePathError):
        inspect_host_path(tmp_path)


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
