"""The real sync lifecycle backed by repository state held in memory."""

from pathlib import Path

import pytest

from guildbotics.sync import activation
from guildbotics.sync.local_repository import LocalSyncRepository, RejectedChange
from guildbotics.sync.manager import GitSyncManager, GitSyncStatus
from guildbotics.workspace.identity import read_workspace_identity

DEFAULT_WORKSPACE_ID = "0198ab00-0000-7000-8000-000000000001"


class MemoryRepository(LocalSyncRepository):
    """Expose only the repository facts lifecycle and presentation tests need."""

    def __init__(self, workspace_root: Path):
        super().__init__(workspace_root)

    @property
    def initialized(self) -> bool:
        return True

    def verify_boundary(self) -> None:
        return None

    def has_remote(self) -> bool:
        return True

    def remote_url(self) -> str:
        return f"memory:///{self.workspace_root.name}"

    def head(self) -> None:
        return None

    def remote_head(self) -> None:
        return None

    def ahead_behind(self, _local: str, _remote: str) -> tuple[int, int]:
        return (0, 0)

    def list_rejected(self) -> tuple[RejectedChange, ...]:
        return ()


class MemoryManager(GitSyncManager):
    """Run the production queue lifecycle without starting Git subprocesses."""

    def __init__(self, workspace_root: Path):
        identity = read_workspace_identity(workspace_root)
        super().__init__(
            MemoryRepository(workspace_root),
            workspace_id=(
                identity.workspace_id if identity is not None else DEFAULT_WORKSPACE_ID
            ),
            device_id="device-test",
            coalesce_delay=0.0,
        )

    def synchronize(self) -> GitSyncStatus:
        self._shared()
        return self.status()

    def resume(self) -> GitSyncStatus:
        return self.synchronize()


def install_memory_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connect activation to a real manager lifecycle and an in-memory port."""

    def connected(workspace_root: Path | None) -> MemoryRepository | None:
        return (
            None if workspace_root is None else MemoryRepository(Path(workspace_root))
        )

    monkeypatch.setattr(activation, "_connected_repository", connected)
    monkeypatch.setattr(activation, "build_git_sync_manager", MemoryManager)
