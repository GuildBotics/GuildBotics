from __future__ import annotations

import json
from pathlib import Path

import pytest

from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.shared_write_lock import shared_write_lock
from guildbotics.utils.shared_writes import (
    delete_shared_path,
    dump_shared_json,
    shared_relative_path,
    write_shared_json,
    write_shared_text,
)


def test_shared_relative_path_covers_config_and_state(tmp_path: Path) -> None:
    assert (
        shared_relative_path(tmp_path / ".guildbotics/config/team/project.yml")
        == "config/team/project.yml"
    )
    assert (
        shared_relative_path(tmp_path / ".guildbotics/state/workspace.json")
        == "state/workspace.json"
    )


def test_shared_relative_path_accepts_an_alias_of_the_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = tmp_path.parent / "workspace-alias"
    target = alias / ".guildbotics/state/workspace.json"
    real_resolve = Path.resolve

    def resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == alias and kwargs.get("strict"):
            return tmp_path
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert shared_relative_path(target) == "state/workspace.json"


def test_shared_relative_path_keeps_dotdot_after_a_resolved_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "outside/link/../thing.json"
    link_parent = tmp_path / "outside/link/.."
    real_resolve = Path.resolve

    def resolve(candidate: Path, *args: object, **kwargs: object) -> Path:
        if candidate == link_parent and kwargs.get("strict"):
            return tmp_path / ".guildbotics/state"
        return real_resolve(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert shared_relative_path(path) == "state/thing.json"


def test_shared_relative_path_follows_a_dangling_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = tmp_path / "alias.json"
    target = tmp_path / ".guildbotics/state/new.json"
    real_is_symlink = Path.is_symlink
    real_readlink = Path.readlink

    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == alias or real_is_symlink(path),
    )
    monkeypatch.setattr(
        Path,
        "readlink",
        lambda path: target if path == alias else real_readlink(path),
    )
    assert shared_relative_path(alias) == "state/new.json"


def test_shared_relative_path_reads_link_even_when_strict_resolution_keeps_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = tmp_path / "alias.json"
    target = tmp_path / ".guildbotics/state/new.json"
    real_resolve = Path.resolve
    real_is_symlink = Path.is_symlink
    real_readlink = Path.readlink

    def resolve(candidate: Path, *args: object, **kwargs: object) -> Path:
        if candidate == alias and kwargs.get("strict"):
            return alias
        return real_resolve(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(
        Path, "is_symlink", lambda path: path == alias or real_is_symlink(path)
    )
    monkeypatch.setattr(
        Path, "readlink", lambda path: target if path == alias else real_readlink(path)
    )
    assert shared_relative_path(alias) == "state/new.json"


def test_shared_relative_path_excludes_device_local_and_outside_paths(
    tmp_path: Path,
) -> None:
    assert shared_relative_path(tmp_path / ".guildbotics/local/run/some.lock") is None
    assert (
        shared_relative_path(tmp_path / ".guildbotics/local/clones/aiko/repo/main.py")
        is None
    )
    assert shared_relative_path(tmp_path / ".guildbotics") is None
    assert shared_relative_path(tmp_path.parent / "elsewhere/state/x.json") is None


def test_shared_relative_path_without_a_selected_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(GUILDBOTICS_WORKSPACE_ROOT, raising=False)
    assert shared_relative_path(tmp_path / ".guildbotics/state/workspace.json") is None


def test_write_shared_json_uses_stable_serialization(tmp_path: Path) -> None:
    path = tmp_path / ".guildbotics/state/task-runs/one.json"
    with shared_write_lock():
        write_shared_json(path, {"b": 2, "a": 1})

    text = path.read_text(encoding="utf-8")
    assert text == '{\n  "a": 1,\n  "b": 2\n}\n'
    assert json.loads(text) == {"a": 1, "b": 2}
    assert dump_shared_json({"b": 2, "a": 1}) == text


@pytest.mark.windows
def test_a_write_replaces_the_file_it_wrote_before(tmp_path: Path) -> None:
    path = tmp_path / ".guildbotics/state/task-runs/one.json"

    # Each save is its own writer, so each declares its own span.
    with shared_write_lock():
        write_shared_json(path, {"a": 1})
    with shared_write_lock():
        write_shared_json(path, {"a": 2})

    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}


def test_delete_removes_an_existing_file_and_tolerates_a_missing_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".guildbotics/state/chat_state/pending/e1.json"
    with shared_write_lock():
        write_shared_text(path, "{}")

    with shared_write_lock():
        delete_shared_path(path)
    assert not path.exists()

    with shared_write_lock():
        delete_shared_path(path)
    assert not path.exists()
