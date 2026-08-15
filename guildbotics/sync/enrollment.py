"""Connecting one workspace to a hub for the first time.

Three things can happen when a workspace meets a hub, and which one it is
follows from what each side already holds rather than from something the user
has to declare:

* the hub has nothing yet, so this workspace becomes its first shared content;
* the hub has content and this workspace has its own, so the two are joined;
* this machine has no workspace at all, so it takes a copy of the hub's.

Joining never overwrites silently. The workspace's existing content is
committed first, so it exists as history before anything is adopted; then the
hub's version wins for every file both sides have, exactly as a concurrent
update would settle, and the losing commit is kept under a rejected ref on this
device with one activity event pointing at it. Files only this machine has are
kept and shared. Nothing asks the user to merge anything by hand.

The preview a user sees before joining runs this same first half, so what it
shows is what will actually happen rather than a separate estimate of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from guildbotics.sync.commits import (
    CommitOutcome,
    UnsendableChange,
    commit_shared_changes,
    validate_received,
)
from guildbotics.sync.local_repository import LocalSyncRepository
from guildbotics.sync.rejections import RejectionRecorder, record_update_rejected
from guildbotics.workspace.identity import (
    WorkspaceIdentity,
    ensure_device_identity,
    ensure_workspace_identity,
    new_uuid7,
    publish_device_record,
)
from guildbotics.workspace.validation import SharedFileInvalidError

_WORKSPACE_IDENTITY_PATH = "state/workspace.json"

EnrollmentMode = Literal["registered", "joined"]


class EnrollmentError(RuntimeError):
    """Raised when a workspace cannot be connected to the hub it was given."""


@dataclass(frozen=True)
class EnrollmentPreview:
    """What joining a hub would do, shown before anything is adopted.

    Attributes:
        hub_workspace_id (str | None): The workspace the hub holds, or None
            when the hub repository is still empty and this workspace would
            become its first content.
        workspace_id (str): This workspace's current identifier. It is replaced
            by ``hub_workspace_id`` when the two differ and the join proceeds.
        hub_only (tuple[str, ...]): Files only the hub has. They are added here.
        device_only (tuple[str, ...]): Files only this machine has. They are
            kept and shared.
        differing (tuple[str, ...]): Files both sides have with different
            content. The hub's version is adopted and this machine's is stashed.
        unsendable (tuple[UnsendableChange, ...]): Local files that cannot be
            shared until they are repaired.
    """

    hub_workspace_id: str | None
    workspace_id: str
    hub_only: tuple[str, ...]
    device_only: tuple[str, ...]
    differing: tuple[str, ...]
    unsendable: tuple[UnsendableChange, ...]


@dataclass(frozen=True)
class EnrollmentResult:
    """What connecting a workspace to a hub actually did.

    Attributes:
        workspace_id (str): The workspace identifier now in force, which is the
            hub's when an existing workspace was joined.
        mode (EnrollmentMode): Whether the hub received its first content or an
            existing workspace was joined.
        adopted (tuple[str, ...]): Files whose content now comes from the hub.
        rejection_id (str | None): Where this machine's losing content is kept,
            or None when nothing was displaced.
        unsendable (tuple[UnsendableChange, ...]): Local files still waiting to
            be repaired before they can be shared.
    """

    workspace_id: str
    mode: EnrollmentMode
    adopted: tuple[str, ...]
    rejection_id: str | None
    unsendable: tuple[UnsendableChange, ...]


def preview_enrollment(
    remote_url: str, workspace_root: Path | None = None
) -> EnrollmentPreview:
    """Report what connecting this workspace to ``remote_url`` would do.

    Nothing about this workspace's connection changes: the hub is read through
    its URL, so a preview the user does not act on leaves a workspace that is
    still not synchronized.
    """
    repository, outcome = _prepare(workspace_root)
    remote = repository.fetch_preview(remote_url)
    local = outcome.head
    if remote is None or local is None:
        return EnrollmentPreview(
            hub_workspace_id=None,
            workspace_id=_local_identity(repository).workspace_id,
            hub_only=(),
            device_only=(),
            differing=(),
            unsendable=outcome.unsendable,
        )
    hub_only, device_only, differing = _classify(repository, local, remote)
    return EnrollmentPreview(
        hub_workspace_id=_hub_identity(repository, remote).workspace_id,
        workspace_id=_local_identity(repository).workspace_id,
        hub_only=hub_only,
        device_only=device_only,
        differing=differing,
        unsendable=outcome.unsendable,
    )


def enroll(
    remote_url: str,
    workspace_root: Path | None = None,
    *,
    record_rejection: RejectionRecorder = record_update_rejected,
) -> EnrollmentResult:
    """Connect this workspace to the hub at ``remote_url``.

    Args:
        remote_url (str): The hub repository for this workspace.
        workspace_root (Path | None): The workspace, or None to use the selected one.
        record_rejection (RejectionRecorder): Records displaced local content.

    Raises:
        EnrollmentError: When the hub holds a repository this workspace cannot
            be reconciled with.
    """
    device_id = ensure_device_identity().device_id
    repository, outcome = _prepare(workspace_root)
    repository.set_remote(remote_url)
    repository.fetch()
    remote = repository.remote_head()
    local = outcome.head
    if remote is None or local is None:
        repository.push()
        result = EnrollmentResult(
            workspace_id=_local_identity(repository).workspace_id,
            mode="registered",
            adopted=(),
            rejection_id=None,
            unsendable=outcome.unsendable,
        )
    else:
        result = _join(
            repository,
            local=local,
            remote=remote,
            device_id=device_id,
            record_rejection=record_rejection,
            unsendable=outcome.unsendable,
        )
    # Published last so the record carries the identity the join settled on,
    # and left for the sync queue to send with everything else.
    publish_device_record(repository.workspace_root)
    return result


def clone_workspace(remote_url: str, workspace_root: Path) -> str:
    """Create a new workspace holding a copy of a hub's shared content.

    Args:
        remote_url (str): The hub repository to copy.
        workspace_root (Path): The new workspace root, which must not already
            hold a ``.guildbotics`` directory.

    Returns:
        str: The identifier of the workspace this machine has now joined.

    Raises:
        EnrollmentError: When the copy does not contain a workspace identity.
    """
    repository = LocalSyncRepository(workspace_root)
    repository.clone(remote_url)
    repository.initialize()
    identity = _local_identity(repository)
    publish_device_record(repository.workspace_root)
    return identity.workspace_id


def _prepare(workspace_root: Path | None) -> tuple[LocalSyncRepository, CommitOutcome]:
    """Make this workspace a repository whose own content is committed.

    The preview and the join share this so that a preview cannot describe a
    different starting point than the join it precedes. Committing here is what
    keeps a join from overwriting anything: the workspace's content exists as
    history, on this device, before the hub's version is adopted over it.
    """
    repository = LocalSyncRepository(workspace_root)
    repository.verify_boundary()
    repository.initialize()
    ensure_workspace_identity(repository.workspace_root)
    outcome = commit_shared_changes(
        repository, device_id=ensure_device_identity().device_id
    )
    return repository, outcome


def _join(
    repository: LocalSyncRepository,
    *,
    local: str,
    remote: str,
    device_id: str,
    record_rejection: RejectionRecorder,
    unsendable: tuple[UnsendableChange, ...],
) -> EnrollmentResult:
    """Adopt the hub's content, keeping what only this machine has."""
    hub_only, _device_only, differing = _classify(repository, local, remote)
    _validate_received(repository, remote, [*hub_only, *differing])
    workspace_id = _hub_identity(repository, remote).workspace_id
    rejection_id = None
    if differing:
        rejection_id = repository.rejected_id_for(local) or new_uuid7()
        repository.save_rejected(rejection_id, local)
        record_rejection(
            rejection_id=rejection_id,
            paths=list(differing),
            device_id=device_id,
            workspace_id=workspace_id,
            workspace_root=repository.workspace_root,
        )
    repository.move_to(remote)
    repository.restore_from_index(sorted([*hub_only, *differing]))
    # What only this machine had is still on disk and no longer tracked, so the
    # commit boundary picks it up again and it travels to the hub next.
    commit_shared_changes(repository, device_id=device_id)
    if repository.head() != remote:
        repository.push()
    return EnrollmentResult(
        workspace_id=workspace_id,
        mode="joined",
        adopted=tuple(sorted([*hub_only, *differing])),
        rejection_id=rejection_id,
        unsendable=unsendable,
    )


def _classify(
    repository: LocalSyncRepository, local: str, remote: str
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Split the difference between two trees into what each side alone holds.

    The two histories are usually unrelated, which a plain comparison of the
    two trees handles: a file added on the way from this machine's tree to the
    hub's exists only on the hub, one deleted exists only here, and a modified
    one exists on both with different content.
    """
    changes = repository.changed_paths(local, remote)
    return (
        tuple(sorted(path for path, status in changes.items() if status == "A")),
        tuple(sorted(path for path, status in changes.items() if status == "D")),
        tuple(sorted(path for path, status in changes.items() if status == "M")),
    )


def _validate_received(
    repository: LocalSyncRepository, remote: str, paths: list[str]
) -> None:
    """Refuse a hub whose content this build cannot read.

    Adopting it would replace working files with content that fails the same
    check on every later synchronization cycle.
    """
    try:
        validate_received(repository, remote, paths)
    except SharedFileInvalidError as exc:
        raise EnrollmentError(
            f"The hub holds shared data this build cannot use: {exc}"
        ) from exc


def _hub_identity(repository: LocalSyncRepository, remote: str) -> WorkspaceIdentity:
    data = repository.read_blob(remote, _WORKSPACE_IDENTITY_PATH)
    if data is None:
        raise EnrollmentError(
            "The hub repository has commits but no workspace identity, so it is "
            "not a GuildBotics workspace."
        )
    return _parse_identity(data)


def _local_identity(repository: LocalSyncRepository) -> WorkspaceIdentity:
    path = repository.path / _WORKSPACE_IDENTITY_PATH
    if not path.is_file():
        raise EnrollmentError(
            f"{_WORKSPACE_IDENTITY_PATH} is missing from this workspace."
        )
    return _parse_identity(path.read_bytes())


def _parse_identity(data: bytes) -> WorkspaceIdentity:
    try:
        return WorkspaceIdentity.model_validate_json(data)
    except ValueError as exc:
        raise EnrollmentError(
            f"{_WORKSPACE_IDENTITY_PATH} is not readable: {exc}"
        ) from exc
