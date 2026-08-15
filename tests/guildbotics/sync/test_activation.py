"""Installing the queue: only for a workspace that has a hub, and only once."""

from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo

from guildbotics.sync import activation, enrollment
from guildbotics.sync.local_repository import LocalSyncRepository
from guildbotics.utils.workspace_sync_port import (
    NoOpWorkspaceSyncPort,
    get_workspace_sync_port,
    set_workspace_sync_port,
    write_shared_text,
)

CONFIG = "config/team/project.yml"


@pytest.fixture(autouse=True)
def _stop_after_each_test() -> None:
    """No test may leave a queue running against its temporary directory."""
    yield
    activation.deactivate_workspace_sync()
    # A test that made a manager refuse to stop still has to release the slot.
    activation._manager = None
    activation._workspace = None
    set_workspace_sync_port(None)


def _workspace(root: Path) -> Path:
    path = root / ".guildbotics" / CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("name: demo\n", encoding="utf-8")
    (root / ".guildbotics" / "state").mkdir(parents=True, exist_ok=True)
    return root


def test_a_workspace_without_a_repository_starts_nothing(tmp_path: Path) -> None:
    """A user who never enables synchronization pays for none of it."""
    assert activation.activate_workspace_sync(_workspace(tmp_path / "mac")) is None
    assert activation.current_sync_manager() is None
    assert isinstance(get_workspace_sync_port(), NoOpWorkspaceSyncPort)


def test_a_repository_without_a_hub_starts_nothing(tmp_path: Path) -> None:
    """A preview that was not acted on leaves exactly this state."""
    root = _workspace(tmp_path / "mac")
    LocalSyncRepository(root).initialize()

    assert activation.activate_workspace_sync(root) is None


def test_an_enrolled_workspace_gets_a_running_queue(tmp_path: Path, hub: Path) -> None:
    root = _workspace(tmp_path / "mac")
    enrollment.enroll(str(hub), root)

    manager = activation.activate_workspace_sync(root)

    assert manager is not None
    assert get_workspace_sync_port() is manager


def test_activating_twice_keeps_the_one_queue(tmp_path: Path, hub: Path) -> None:
    root = _workspace(tmp_path / "mac")
    enrollment.enroll(str(hub), root)

    first = activation.activate_workspace_sync(root)

    assert activation.activate_workspace_sync(root) is first


def test_switching_workspaces_replaces_the_queue(tmp_path: Path, hub: Path) -> None:
    first_root = _workspace(tmp_path / "mac")
    enrollment.enroll(str(hub), first_root)
    other_hub = tmp_path / "other-hub.git"
    Repo.init(other_hub, bare=True, initial_branch="main")
    second_root = _workspace(tmp_path / "windows")
    enrollment.enroll(str(other_hub), second_root)

    first = activation.activate_workspace_sync(first_root)
    second = activation.activate_workspace_sync(second_root)

    assert second is not first
    assert get_workspace_sync_port() is second


def test_deactivating_restores_the_port_that_does_nothing(
    tmp_path: Path, hub: Path
) -> None:
    root = _workspace(tmp_path / "mac")
    enrollment.enroll(str(hub), root)
    activation.activate_workspace_sync(root)

    activation.deactivate_workspace_sync()

    assert activation.current_sync_manager() is None
    assert isinstance(get_workspace_sync_port(), NoOpWorkspaceSyncPort)


def test_a_shared_write_reaches_the_running_queue(tmp_path: Path, hub: Path) -> None:
    """The whole point of installing it: storage layers announce, Git follows."""
    root = _workspace(tmp_path / "mac")
    enrollment.enroll(str(hub), root)
    manager = activation.activate_workspace_sync(root)
    assert manager is not None

    change = write_shared_text(
        root / ".guildbotics" / "config" / "hotkeys.yml",
        "a: b\n",
        workspace_root=root,
    )

    assert change is not None
    assert manager.await_pushed(change.change_id) is True
    assert Repo(hub).git.cat_file("blob", "main:config/hotkeys.yml") == "a: b"


def test_a_queue_that_will_not_stop_blocks_the_next_workspace(
    tmp_path: Path, hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two threads on one repository interleave reset, checkout, and commit,
    and a switch that went ahead anyway would leave the old one running."""
    first_root = _workspace(tmp_path / "mac")
    enrollment.enroll(str(hub), first_root)
    other_hub = tmp_path / "other-hub.git"
    Repo.init(other_hub, bare=True, initial_branch="main")
    second_root = _workspace(tmp_path / "windows")
    enrollment.enroll(str(other_hub), second_root)
    stuck = activation.activate_workspace_sync(first_root)
    assert stuck is not None
    monkeypatch.setattr(stuck, "stop", lambda timeout=5.0: False)

    with pytest.raises(activation.SyncStillStoppingError):
        activation.activate_workspace_sync(second_root)

    assert activation.current_sync_manager() is stuck


def test_a_queue_that_will_not_stop_keeps_receiving_writes(
    tmp_path: Path, hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detaching the port from a worker that is still committing would leave
    its own workspace's saves announced to nobody."""
    root = _workspace(tmp_path / "mac")
    enrollment.enroll(str(hub), root)
    manager = activation.activate_workspace_sync(root)
    assert manager is not None
    monkeypatch.setattr(manager, "stop", lambda timeout=5.0: False)

    assert activation.deactivate_workspace_sync() is False
    assert get_workspace_sync_port() is manager


def test_reactivating_the_same_workspace_reuses_its_queue(
    tmp_path: Path, hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is that workspace's own queue, so there is nothing to make room for."""
    root = _workspace(tmp_path / "mac")
    enrollment.enroll(str(hub), root)
    manager = activation.activate_workspace_sync(root)
    assert manager is not None
    monkeypatch.setattr(manager, "stop", lambda timeout=5.0: False)

    assert activation.activate_workspace_sync(root) is manager
