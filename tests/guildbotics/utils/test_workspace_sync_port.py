from __future__ import annotations

import json
from pathlib import Path

import pytest

from guildbotics.utils import workspace_sync_port
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.workspace_sync_port import (
    ChangeSet,
    NoOpWorkspaceSyncPort,
    delete_shared_path,
    dump_shared_json,
    notify_shared_state_changed,
    shared_relative_path,
    write_shared_json,
    write_shared_text,
)


class RecordingPort:
    """A sync port that keeps every announcement for inspection."""

    def __init__(self) -> None:
        self.changes: list[ChangeSet] = []
        self.awaited: list[str] = []
        self.pushed = True

    def shared_state_changed(self, change: ChangeSet) -> bool:
        self.changes.append(change)
        return True

    def await_pushed(self, change_id: str) -> bool:
        self.awaited.append(change_id)
        return self.pushed


@pytest.fixture
def port(monkeypatch: pytest.MonkeyPatch) -> RecordingPort:
    recording = RecordingPort()
    monkeypatch.setattr(workspace_sync_port, "_port", recording)
    return recording


def test_shared_relative_path_covers_config_and_state(tmp_path: Path) -> None:
    assert (
        shared_relative_path(tmp_path / ".guildbotics/config/team/project.yml")
        == "config/team/project.yml"
    )
    assert (
        shared_relative_path(tmp_path / ".guildbotics/state/workspace.json")
        == "state/workspace.json"
    )


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


def test_write_shared_json_uses_stable_serialization(
    tmp_path: Path, port: RecordingPort
) -> None:
    path = tmp_path / ".guildbotics/state/devices/one.json"
    write_shared_json(path, {"b": 2, "a": 1})

    text = path.read_text(encoding="utf-8")
    assert text == '{\n  "a": 1,\n  "b": 2\n}\n'
    assert json.loads(text) == {"a": 1, "b": 2}
    assert dump_shared_json({"b": 2, "a": 1}) == text


def test_write_announces_create_then_update(
    tmp_path: Path, port: RecordingPort
) -> None:
    path = tmp_path / ".guildbotics/state/devices/one.json"

    write_shared_json(path, {"a": 1})
    write_shared_json(path, {"a": 2})

    assert [change.operation for change in port.changes] == ["create", "update"]
    assert [change.paths for change in port.changes] == [
        ("state/devices/one.json",),
        ("state/devices/one.json",),
    ]
    assert len({change.change_id for change in port.changes}) == 2


def test_delete_announces_only_for_an_existing_file(
    tmp_path: Path, port: RecordingPort
) -> None:
    path = tmp_path / ".guildbotics/state/chat_state/pending/e1.json"
    write_shared_text(path, "{}")
    port.changes.clear()

    assert delete_shared_path(path) is not None
    assert not path.exists()
    assert [change.operation for change in port.changes] == ["delete"]

    assert delete_shared_path(path) is None
    assert len(port.changes) == 1


def test_device_local_writes_are_not_announced(
    tmp_path: Path, port: RecordingPort
) -> None:
    write_shared_text(tmp_path / ".guildbotics/local/chat-cache/thread.json", "{}")

    assert port.changes == []


def test_notify_drops_local_paths_and_keeps_shared_ones(
    tmp_path: Path, port: RecordingPort
) -> None:
    change = notify_shared_state_changed(
        "update",
        [
            tmp_path / ".guildbotics/local/chat-cache/thread.json",
            tmp_path / ".guildbotics/state/documents/team/abc/meta.yml",
        ],
    )

    assert change is not None
    assert change.paths == ("state/documents/team/abc/meta.yml",)


def test_notify_returns_none_when_nothing_shared_changed(
    tmp_path: Path, port: RecordingPort
) -> None:
    assert (
        notify_shared_state_changed(
            "update", [tmp_path / ".guildbotics/local/run/service.lock"]
        )
        is None
    )
    assert port.changes == []


def test_the_default_port_accepts_nothing_and_never_raises() -> None:
    default = NoOpWorkspaceSyncPort()
    change = ChangeSet(change_id="c1", operation="update", paths=("state/x.json",))

    assert default.shared_state_changed(change) is False
    assert default.await_pushed("c1") is False
