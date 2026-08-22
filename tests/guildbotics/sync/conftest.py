"""Two workspaces and a bare hub, which is what synchronization actually needs.

Every behaviour under test -- convergence, rejection, recovery, the barrier --
only exists between two devices, so the fixtures build real repositories on
disk rather than mocking Git. The hub refuses non-fast-forward pushes exactly
as a real one does, because that refusal is what serializes shared state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from git import Repo

from guildbotics.sync.local_repository import LocalSyncRepository
from guildbotics.sync.manager import GitSyncManager
from guildbotics.utils.workspace_sync_port import ChangeSet, dump_shared_json
from guildbotics.workspace.identity import WorkspaceIdentity, new_uuid7

WORKSPACE_ID = "0198ab00-0000-7000-8000-000000000001"


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
        path = self.shared / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
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
def hub(tmp_path: Path) -> Path:
    """A bare repository that accepts fast-forward pushes only."""
    path = tmp_path / "hub.git"
    repository = Repo.init(path, bare=True, initial_branch="main")
    repository.git.config("receive.denyNonFastForwards", "true")
    return path


def make_device(
    root: Path,
    hub: Path,
    *,
    device_id: str,
    workspace_id: str = WORKSPACE_ID,
    **manager_options: Any,
) -> Device:
    """Create a workspace connected to ``hub`` with synchronization ready."""
    (root / ".guildbotics" / "config").mkdir(parents=True, exist_ok=True)
    (root / ".guildbotics" / "state").mkdir(parents=True, exist_ok=True)
    (root / ".guildbotics" / "local").mkdir(parents=True, exist_ok=True)
    repository = LocalSyncRepository(root)
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
    return Device(
        root=root, repository=repository, manager=manager, rejections=rejections
    )


@pytest.fixture
def first(tmp_path: Path, hub: Path) -> Device:
    """The device that publishes the workspace identity, as setup does."""
    device = make_device(tmp_path / "mac", hub, device_id="device-mac")
    device.write(
        "state/workspace.json",
        dump_shared_json(
            WorkspaceIdentity(
                workspace_id=WORKSPACE_ID, created_at="2026-08-01T00:00:00Z"
            ).model_dump()
        ),
    )
    device.manager.synchronize()
    return device


@pytest.fixture
def second(tmp_path: Path, hub: Path, first: Device) -> Device:
    """A device that has already taken the workspace from the hub."""
    device = make_device(tmp_path / "windows", hub, device_id="device-windows")
    device.manager.synchronize()
    return device
