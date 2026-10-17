"""Git synchronization of the shared workspace state between the user's machines.

``<workspace>/.guildbotics`` is its own Git repository: ``config/`` and
``state/`` are tracked and travel between devices, ``local/`` never leaves the
machine. :class:`~guildbotics.sync.manager.GitSyncManager` is the single
subscriber of the Workspace Sync Port, so no capability, command runner, or API
layer learns that Git is involved. The members' identities, which every
workspace shares, are a repository of the same shape under
:func:`~guildbotics.utils.fileio.get_identity_root`, synchronized by a
queue of their own beside the workspace's, started by
:func:`~guildbotics.sync.activation.activate_identity_sync`.

The package sits above workspace storage and observability and below the
capability, driver, and API layers. Only a process entry point imports it, and
only to install the queue with
:func:`~guildbotics.sync.activation.activate_workspace_sync`; everything else
reaches synchronization through the port.
"""

from __future__ import annotations

from guildbotics.sync.activation import (
    SyncStillStoppingError,
    activate_identity_sync,
    activate_workspace_sync,
    current_sync_manager,
    deactivate_identity_sync,
    deactivate_workspace_sync,
    hub_remote_url,
    paused_identity_sync,
    paused_workspace_sync,
    run_current_sync,
    synchronize_once,
)
from guildbotics.sync.commits import UnsendableChange
from guildbotics.sync.enrollment import (
    EnrollmentError,
    EnrollmentMode,
    EnrollmentPreview,
    EnrollmentResult,
    PreviewMode,
    clone_workspace,
    enroll,
    preview_enrollment,
)
from guildbotics.sync.local_repository import (
    LocalSyncRepository,
    RejectedChange,
    SyncRepositoryError,
)
from guildbotics.sync.manager import (
    GitSyncManager,
    GitSyncStatus,
    SharedDataAnomaly,
    SyncState,
    build_git_sync_manager,
)
from guildbotics.sync.rejections import record_update_rejected

__all__ = [
    "EnrollmentError",
    "EnrollmentMode",
    "EnrollmentPreview",
    "EnrollmentResult",
    "GitSyncManager",
    "GitSyncStatus",
    "LocalSyncRepository",
    "PreviewMode",
    "RejectedChange",
    "SharedDataAnomaly",
    "SyncRepositoryError",
    "SyncState",
    "SyncStillStoppingError",
    "UnsendableChange",
    "activate_identity_sync",
    "activate_workspace_sync",
    "build_git_sync_manager",
    "clone_workspace",
    "current_sync_manager",
    "deactivate_identity_sync",
    "deactivate_workspace_sync",
    "enroll",
    "hub_remote_url",
    "paused_identity_sync",
    "paused_workspace_sync",
    "preview_enrollment",
    "record_update_rejected",
    "run_current_sync",
    "synchronize_once",
]
