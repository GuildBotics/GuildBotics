"""Git synchronization of the shared workspace state between the user's machines.

``<workspace>/.guildbotics`` is its own Git repository: ``config/`` and
``state/`` are tracked and travel between devices, ``local/`` never leaves the
machine. :class:`~guildbotics.sync.manager.GitSyncManager` is the single
subscriber of the Workspace Sync Port, so no capability, command runner, or API
layer learns that Git is involved.

The package sits above workspace storage and observability and below the
capability, driver, and API layers.
"""

from __future__ import annotations

from guildbotics.sync.local_repository import (
    LocalSyncRepository,
    SyncRepositoryError,
    WorkingTreeChange,
)
from guildbotics.sync.manager import (
    GitSyncManager,
    GitSyncStatus,
    SharedDataAnomaly,
    SyncState,
    UnsendableChange,
    build_git_sync_manager,
)
from guildbotics.sync.rejections import record_update_rejected

__all__ = [
    "GitSyncManager",
    "GitSyncStatus",
    "LocalSyncRepository",
    "SharedDataAnomaly",
    "SyncRepositoryError",
    "SyncState",
    "UnsendableChange",
    "WorkingTreeChange",
    "build_git_sync_manager",
    "record_update_rejected",
]
