"""Two workspaces and a bare hub, which is what synchronization actually needs.

Every behaviour under test -- convergence, rejection, recovery, the barrier --
only exists between two devices, so the fixtures build real repositories on
disk rather than mocking Git. The hub refuses non-fast-forward pushes exactly
as a real one does, because that refusal is what serializes shared state.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from shutil import copytree
from typing import Any

import pytest

from guildbotics.sync.local_repository import LocalSyncRepository
from guildbotics.sync.manager import GitSyncManager
from guildbotics.utils.workspace_sync_port import ChangeSet
from guildbotics.workspace.identity import new_uuid7
from tests.git_seed import WORKSPACE_ID, WorkerGitSeed

#: Every manager a test built, so no worker can outlive its test.
_LIVE_MANAGERS: list[GitSyncManager] = []


@pytest.fixture(autouse=True)
def _no_worker_outlives_its_test() -> Iterator[None]:
    """Stop every queue this test built, however the test left it.

    A surviving worker keeps cycling on its own timer and walks into a later
    test's class-wide spies as a flaky extra observation rather than a clear
    failure. The class's own stop runs even when the test patched ``stop`` on
    the instance to simulate one that refuses.
    """
    yield
    while _LIVE_MANAGERS:
        manager = _LIVE_MANAGERS.pop()
        assert GitSyncManager.stop(manager, timeout=10), (
            "a synchronization worker outlived its test"
        )


@dataclass
class Device:
    """One machine's workspace, its repository, and its sync manager."""

    root: Path
    repository: LocalSyncRepository
    manager: GitSyncManager
    rejections: list[dict[str, Any]] = field(default_factory=list)

    @property
    def shared(self) -> Path:
        return self.root / ".guildbotics"

    def write(self, relative: str, text: str) -> ChangeSet:
        """Write a shared file and announce it the way a storage layer would."""
        self.write_bytes(relative, text.encode("utf-8"))
        change = ChangeSet(
            change_id=new_uuid7(),
            operation="update",
            paths=(relative,),
        )
        self.manager.shared_state_changed(change)
        return change

    def write_bytes(self, relative: str, data: bytes) -> None:
        path = self.shared / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def delete(self, relative: str) -> ChangeSet:
        (self.shared / relative).unlink()
        change = ChangeSet(change_id=new_uuid7(), operation="delete", paths=(relative,))
        self.manager.shared_state_changed(change)
        return change

    def read(self, relative: str) -> str:
        return (self.shared / relative).read_text(encoding="utf-8")

    def exists(self, relative: str) -> bool:
        return (self.shared / relative).exists()


@pytest.fixture
def hub(tmp_path: Path, worker_git_seed: WorkerGitSeed) -> Path:
    """A bare repository that accepts fast-forward pushes only."""
    path = tmp_path / "hub.git"
    worker_git_seed.copy(worker_git_seed.empty_hub, path)
    return path


def make_device(
    root: Path,
    hub: Path,
    *,
    device_id: str,
    workspace_id: str = WORKSPACE_ID,
    seed: Path | None = None,
    **manager_options: Any,
) -> Device:
    """Create a workspace connected to ``hub`` with synchronization ready."""
    if seed is not None:
        copytree(seed, root / ".guildbotics")
    (root / ".guildbotics" / "config").mkdir(parents=True, exist_ok=True)
    (root / ".guildbotics" / "state").mkdir(parents=True, exist_ok=True)
    (root / ".guildbotics" / "local").mkdir(parents=True, exist_ok=True)
    repository = LocalSyncRepository(root)
    if seed is None:
        repository.initialize()
    repository.set_remote(str(hub))
    rejections: list[dict[str, Any]] = []
    manager = GitSyncManager(
        repository,
        workspace_id=workspace_id,
        device_id=device_id,
        record_rejection=lambda **fields: rejections.append(fields),
        coalesce_delay=0.0,
        push_barrier_timeout=manager_options.pop("push_barrier_timeout", 2.0),
        **manager_options,
    )
    _LIVE_MANAGERS.append(manager)
    return Device(
        root=root, repository=repository, manager=manager, rejections=rejections
    )


@pytest.fixture
def first(tmp_path: Path, hub: Path, worker_git_seed: WorkerGitSeed) -> Device:
    """The device that publishes the workspace identity, as setup does."""
    device = make_device(
        tmp_path / "mac",
        hub,
        device_id="device-mac",
        seed=worker_git_seed.sync_device,
    )
    device.repository.push()
    # The original first-device setup synchronized an announced identity
    # change. ``synchronize()`` settled it but deliberately left the wake event
    # set, so a subsequently started worker ran its first cycle immediately.
    device.manager.wake()
    return device


@pytest.fixture
def second(
    tmp_path: Path,
    hub: Path,
    first: Device,
    worker_git_seed: WorkerGitSeed,
) -> Device:
    """A device that has already taken the workspace from the hub."""
    return make_device(
        tmp_path / "windows",
        hub,
        device_id="device-windows",
        seed=worker_git_seed.sync_device,
    )
