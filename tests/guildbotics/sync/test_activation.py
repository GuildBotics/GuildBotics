"""Installing the queue: only for a workspace that has a hub, and only once."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from git import Repo

from guildbotics.sync import activation, enrollment
from guildbotics.sync.local_repository import GITIGNORE_CONTENT, LocalSyncRepository
from guildbotics.sync.manager import GitSyncManager
from guildbotics.utils import sync_lock as sync_lock_module
from guildbotics.utils.advisory_lock import held_lock
from guildbotics.utils.shared_write_lock import shared_write_lock
from guildbotics.utils.sync_lock import SyncRepositoryBusyError, sync_lock_path
from guildbotics.utils.workspace_sync_port import (
    NoOpWorkspaceSyncPort,
    get_workspace_sync_port,
    set_workspace_sync_port,
    write_shared_text,
)
from tests.guildbotics.sync.fake_activation import install_memory_activation

CONFIG = "config/team/project.yml"


@pytest.fixture
def memory_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    install_memory_activation(monkeypatch)


@pytest.fixture(autouse=True)
def _stop_after_each_test() -> None:
    """No test may leave a queue running against its temporary directory."""
    yield
    if not activation.deactivate_workspace_sync():
        # The test patched stop() to refuse; the real worker must still stop,
        # or it keeps cycling on its own timer and walks into a later test's
        # class-wide spies. The slot is then released by hand.
        manager = activation.current_sync_manager()
        if manager is not None:
            assert GitSyncManager.stop(manager, timeout=10), (
                "a synchronization worker outlived its test"
            )
        activation._manager = None
        activation._workspace = None
        set_workspace_sync_port(None)


def _workspace(root: Path) -> Path:
    path = root / ".guildbotics" / CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"name: demo\n")
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


def test_activation_refreshes_generated_ignore_rules(tmp_path: Path, hub: Path) -> None:
    root = _workspace(tmp_path / "mac")
    enrollment.enroll(str(hub), root)
    ignore = root / ".guildbotics" / ".gitignore"
    ignore.write_text("old rules\n")

    manager = activation.activate_workspace_sync(root)

    assert manager is not None
    assert ignore.read_text() == GITIGNORE_CONTENT


def test_activating_twice_keeps_the_one_queue(
    tmp_path: Path, memory_activation: None
) -> None:
    root = _workspace(tmp_path / "mac")

    first = activation.activate_workspace_sync(root)

    assert activation.activate_workspace_sync(root) is first


def test_switching_workspaces_replaces_the_queue(
    tmp_path: Path, memory_activation: None
) -> None:
    first_root = _workspace(tmp_path / "mac")
    second_root = _workspace(tmp_path / "windows")

    first = activation.activate_workspace_sync(first_root)
    second = activation.activate_workspace_sync(second_root)

    assert second is not first
    assert get_workspace_sync_port() is second


def test_deactivating_restores_the_port_that_does_nothing(
    tmp_path: Path, memory_activation: None
) -> None:
    root = _workspace(tmp_path / "mac")
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

    # A storage layer declares its span around the write; that is all it knows.
    with shared_write_lock(root):
        change = write_shared_text(
            root / ".guildbotics" / "config" / "roles" / "reviewer.yml",
            "a: b\n",
            workspace_root=root,
        )

    assert change is not None
    assert manager.await_pushed(change.change_id) is True
    assert Repo(hub).git.cat_file("blob", "main:config/roles/reviewer.yml") == "a: b"


def test_a_queue_that_will_not_stop_blocks_the_next_workspace(
    tmp_path: Path,
    memory_activation: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two threads on one repository interleave reset, checkout, and commit,
    and a switch that went ahead anyway would leave the old one running."""
    first_root = _workspace(tmp_path / "mac")
    second_root = _workspace(tmp_path / "windows")
    stuck = activation.activate_workspace_sync(first_root)
    assert stuck is not None
    monkeypatch.setattr(stuck, "stop", lambda timeout=5.0: False)

    with pytest.raises(activation.SyncStillStoppingError):
        activation.activate_workspace_sync(second_root)

    assert activation.current_sync_manager() is stuck


def test_a_queue_that_will_not_stop_keeps_receiving_writes(
    tmp_path: Path,
    memory_activation: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detaching the port from a worker that is still committing would leave
    its own workspace's saves announced to nobody."""
    root = _workspace(tmp_path / "mac")
    manager = activation.activate_workspace_sync(root)
    assert manager is not None
    monkeypatch.setattr(manager, "stop", lambda timeout=5.0: False)

    assert activation.deactivate_workspace_sync() is False
    assert get_workspace_sync_port() is manager


def test_reactivating_the_same_workspace_reuses_its_queue(
    tmp_path: Path,
    memory_activation: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It is that workspace's own queue, so there is nothing to make room for."""
    root = _workspace(tmp_path / "mac")
    manager = activation.activate_workspace_sync(root)
    assert manager is not None
    monkeypatch.setattr(manager, "stop", lambda timeout=5.0: False)

    assert activation.activate_workspace_sync(root) is manager


def test_a_pause_keeps_every_other_activation_out_for_its_whole_body(
    tmp_path: Path, memory_activation: None
) -> None:
    """Holding the lock only across the stop would let a second request see no
    manager and walk into the same repository, or start a queue beside the
    work in progress."""
    root = _workspace(tmp_path / "mac")
    activation.activate_workspace_sync(root)
    order: list[str] = []
    inside = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with activation.paused_workspace_sync(root):
            order.append("paused")
            inside.set()
            release.wait(5)
        order.append("resumed")

    def contend() -> None:
        activation.activate_workspace_sync(root)
        order.append("activated")

    holder = threading.Thread(target=hold)
    holder.start()
    assert inside.wait(5)
    other = threading.Thread(target=contend)
    other.start()
    other.join(0.2)

    assert other.is_alive(), "a second activation entered while the pause was held"
    release.set()
    holder.join(5)
    other.join(5)
    assert order == ["paused", "resumed", "activated"]


def test_a_pause_restores_the_queue_after_the_body_fails(
    tmp_path: Path, memory_activation: None
) -> None:
    root = _workspace(tmp_path / "mac")
    activation.activate_workspace_sync(root)

    with pytest.raises(RuntimeError), activation.paused_workspace_sync(root):
        assert activation.current_sync_manager() is None
        raise RuntimeError("enrollment failed")

    assert activation.current_sync_manager() is not None


def test_a_pause_that_cannot_take_the_lock_restores_the_queue(
    tmp_path: Path,
    memory_activation: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy repository lock stops the pause, not the synchronization.

    The queue is stopped before the lock is tried, so answering busy without
    restarting it would leave a workspace that has a hub quietly not
    synchronizing.
    """
    root = _workspace(tmp_path / "mac")
    activation.activate_workspace_sync(root)
    monkeypatch.setattr(sync_lock_module, "LOCK_TIMEOUT_SECONDS", 0.01)

    with held_lock(sync_lock_path(root), timeout=0.0):
        with pytest.raises(SyncRepositoryBusyError):
            with activation.paused_workspace_sync(root):
                raise AssertionError("the pause must not begin")

    assert activation.current_sync_manager() is not None


def test_reactivating_revives_a_worker_that_died_after_a_timed_out_stop(
    tmp_path: Path, memory_activation: None
) -> None:
    """stop() withdraws a timed-out request, but a worker that observed it
    before the withdrawal exits anyway, leaving the registered manager without
    a worker. Activation is the pass every caller repairs that on."""
    root = _workspace(tmp_path / "mac")
    manager = activation.activate_workspace_sync(root)
    assert manager is not None
    # The race, made deterministic: the worker sees the stop request and exits
    # while the manager stays registered for this workspace.
    manager._stopping.set()  # noqa: SLF001
    manager._wake.set()  # noqa: SLF001
    worker = manager._worker  # noqa: SLF001
    assert worker is not None
    worker.join(10)
    assert not worker.is_alive()

    assert activation.activate_workspace_sync(root) is manager

    revived = manager._worker  # noqa: SLF001
    assert revived is not None and revived.is_alive()


def test_a_current_operation_returns_nothing_when_no_queue_is_running() -> None:
    assert activation.run_current_sync(lambda _manager, root: root) is None


def test_a_current_operation_keeps_a_workspace_switch_out_until_it_finishes(
    tmp_path: Path, memory_activation: None
) -> None:
    """Manager and workspace stay one selection for the whole operation."""
    first_root = _workspace(tmp_path / "mac")
    second_root = _workspace(tmp_path / "windows")
    first = activation.activate_workspace_sync(first_root)
    assert first is not None
    inside = threading.Event()
    release = threading.Event()

    def operation(manager: GitSyncManager, root: Path) -> tuple[GitSyncManager, Path]:
        inside.set()
        assert release.wait(5)
        return manager, root

    result: list[tuple[GitSyncManager, Path] | None] = []

    def run() -> None:
        result.append(activation.run_current_sync(operation))

    runner = threading.Thread(target=run)
    runner.start()
    assert inside.wait(5)

    def switch() -> None:
        activation.activate_workspace_sync(second_root)

    switcher = threading.Thread(target=switch)
    switcher.start()
    switcher.join(0.2)

    assert switcher.is_alive(), "a workspace switch entered during the operation"
    release.set()
    runner.join(5)
    switcher.join(5)

    assert result == [(first, first_root)]
    assert activation.current_sync_manager() is not first
