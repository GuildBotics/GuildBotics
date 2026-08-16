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

Joining compares two whole trees, which is only the right question when the two
sides share no history. A workspace reconnecting to a rebuilt hub does share
history with it, and there "both sides have this file" says nothing -- what
matters is who changed it since the commit they last agreed on. That case is
handed to the ordinary synchronization rules instead of being decided here.

The preview a user sees runs the same first half and the same classification,
so what it shows is what will actually happen rather than a separate estimate
of it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from git import GitCommandError

from guildbotics.sync.commits import (
    CommitOutcome,
    UnsendableChange,
    commit_shared_changes,
    validate_received,
)
from guildbotics.sync.local_repository import LocalSyncRepository, SyncRepositoryError
from guildbotics.sync.manager import GitSyncManager
from guildbotics.sync.rejections import RejectionRecorder, record_update_rejected
from guildbotics.utils.shared_write_lock import shared_write_lock
from guildbotics.workspace.identity import (
    WorkspaceIdentity,
    ensure_device_identity,
    ensure_workspace_identity,
    new_uuid7,
    publish_device_record,
)
from guildbotics.workspace.validation import SharedFileInvalidError

_WORKSPACE_IDENTITY_PATH = "state/workspace.json"

#: What connecting this workspace to a hub turns out to be. ``register`` gives
#: the hub its first content, ``join`` merges two histories that never met, and
#: ``reconnect`` settles a hub this workspace already shares history with. Only
#: the last two are ever previewed; the first has nothing to compare against.
EnrollmentMode = Literal["register", "join", "reconnect"]
#: The outcomes a preview can describe, which is every one but ``register``.
PreviewMode = Literal["join", "reconnect"]


class EnrollmentError(RuntimeError):
    """Raised when a workspace cannot be connected to the hub it was given."""


@dataclass(frozen=True)
class EnrollmentPreview:
    """What joining a hub would do, shown before anything is adopted.

    Attributes:
        hub_workspace_id (str): The workspace the hub holds. It is adopted here
            when the join proceeds and the two identifiers differ.
        mode (PreviewMode): What connecting would turn out to be.
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

    hub_workspace_id: str
    mode: PreviewMode
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
    """Report what joining the workspace the hub holds at ``remote_url`` would do.

    Only joining has anything to preview. Registering gives a hub its first
    content, so there is no other side to compare against -- and asking for one
    anyway would make this workspace a repository for an answer that is known
    in advance.

    Nothing about this workspace's connection changes: the hub is read through
    its URL, so a preview the user does not act on leaves a workspace that is
    still not synchronized.

    Raises:
        EnrollmentError: When the hub cannot be reached, cannot be read, or
            does not hold this workspace.
    """
    repository, outcome = _prepare(workspace_root)
    with _as_enrollment_error("The hub could not be read"):
        remote = repository.fetch_preview(remote_url)
        try:
            return _preview(repository, outcome, remote)
        finally:
            # A hub the user decided not to join keeps no content here.
            repository.forget_preview()


def _preview(
    repository: LocalSyncRepository, outcome: CommitOutcome, remote: str | None
) -> EnrollmentPreview:
    local = outcome.head
    if remote is None or local is None:
        raise EnrollmentError(
            "The hub does not hold this workspace, so there is nothing to compare."
        )
    hub_only, device_only, differing = _classify(repository, local, remote)
    return EnrollmentPreview(
        hub_workspace_id=_hub_identity(repository, remote).workspace_id,
        mode=_mode(repository, local, remote),
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
        EnrollmentError: When the hub cannot be reached, or holds a repository
            this workspace cannot be reconciled with. The hub this workspace was
            connected to beforehand -- none, or the previous one -- is restored,
            so a refused hub never becomes the one the queue works against.
    """
    device_id = ensure_device_identity().device_id
    repository, outcome = _prepare(workspace_root)
    previous_url = repository.remote_url()
    with _as_enrollment_error("The hub could not be reached"):
        repository.set_remote(remote_url)
        try:
            result = _connect(
                repository,
                outcome,
                device_id=device_id,
                record_rejection=record_rejection,
            )
        except Exception:
            # Put back exactly what was there. Clearing unconditionally would
            # disconnect a workspace whose current hub is fine, and leaving the
            # new URL would point the queue at a hub that was just refused --
            # which is the case when a hub is being changed rather than set.
            if previous_url is None:
                repository.clear_remote()
            else:
                repository.set_remote(previous_url)
            raise
    # Published last so the record carries the identity the join settled on,
    # and left for the sync queue to send with everything else.
    publish_device_record(repository.workspace_root)
    return result


def _connect(
    repository: LocalSyncRepository,
    outcome: CommitOutcome,
    *,
    device_id: str,
    record_rejection: RejectionRecorder,
) -> EnrollmentResult:
    """Decide which of the three things this connection is, and do it."""
    repository.fetch()
    remote = repository.remote_head()
    local = outcome.head
    if remote is None or local is None:
        repository.push()
        return EnrollmentResult(
            workspace_id=_local_identity(repository).workspace_id,
            mode="register",
            adopted=(),
            rejection_id=None,
            unsendable=outcome.unsendable,
        )
    if _mode(repository, local, remote) == "reconnect":
        return _reconnect(
            repository, device_id=device_id, record_rejection=record_rejection
        )
    return _join(
        repository,
        local=local,
        remote=remote,
        device_id=device_id,
        record_rejection=record_rejection,
        unsendable=outcome.unsendable,
    )


def _reconnect(
    repository: LocalSyncRepository,
    *,
    device_id: str,
    record_rejection: RejectionRecorder,
) -> EnrollmentResult:
    """Settle a hub this workspace already shares history with.

    Comparing the two trees would be the wrong question here: a file both sides
    have is not a conflict unless both changed it since the commit they last
    agreed on. That is exactly what ordinary synchronization decides, so this
    runs one cycle of it rather than deciding again with a different rule.
    """
    identity = _local_identity(repository)
    rejected: list[str] = []

    def capture(**fields: object) -> None:
        rejected.append(str(fields["rejection_id"]))
        record_rejection(**fields)

    status = GitSyncManager(
        repository,
        workspace_id=identity.workspace_id,
        device_id=device_id,
        record_rejection=capture,
    ).synchronize()
    if status.state != "idle" or status.last_error_code is not None:
        raise EnrollmentError(
            f"The hub was reached but not settled with: {status.last_error_code}"
        )
    return EnrollmentResult(
        workspace_id=identity.workspace_id,
        mode="reconnect",
        adopted=(),
        rejection_id=rejected[0] if rejected else None,
        unsendable=status.invalid_paths,
    )


def clone_workspace(remote_url: str, workspace_root: Path) -> str:
    """Create a new workspace holding a copy of a hub's shared content.

    Args:
        remote_url (str): The hub repository to copy.
        workspace_root (Path): The new workspace root, which must not already
            hold a ``.guildbotics`` directory.

    Returns:
        str: The identifier of the workspace this machine has now joined.

    Raises:
        EnrollmentError: When the hub cannot be reached, or the copy does not
            contain a workspace identity.
    """
    repository = LocalSyncRepository(workspace_root)
    with _as_enrollment_error("The workspace could not be taken from the hub"):
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
    history, on this device, before the hub's version is adopted over it. The
    join commits once more inside its own lock, for whatever was saved while
    the hub was being reached.
    """
    repository = LocalSyncRepository(workspace_root)
    repository.verify_boundary()
    repository.initialize()
    ensure_workspace_identity(repository.workspace_root)
    with shared_write_lock(repository.workspace_root):
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
    """Adopt the hub's content, keeping what only this machine has.

    Held under the shared-write lock from end to end. Reaching the hub had to
    happen without it, and a save made in that interval holds the lock
    correctly and is still only in the working tree, so the checkout below
    would take it away -- and being uncommitted it would not be rejected on the
    record either. Running the commit boundary again as the first thing inside
    the lock turns it into a change with a name, which either survives the
    adoption or is rejected; everything after is decided against the head that
    produced rather than the one this was called with. Nothing in here waits on
    the hub, so there is no reason to give the lock up part-way.
    """
    with shared_write_lock(repository.workspace_root):
        outcome = commit_shared_changes(repository, device_id=device_id)
        # The boundary reports no head only for a repository with no commits,
        # and this one has them -- ``local`` is the head _prepare committed.
        local = outcome.head or local
        unsendable = outcome.unsendable
        hub_only, _device_only, differing = _classify(repository, local, remote)
        _validate_received(repository, remote, [*hub_only, *differing])
        workspace_id = _hub_identity(repository, remote).workspace_id
        rejection_id = repository.rejected_id_for(local)
        if differing and rejection_id is None:
            rejection_id = new_uuid7()
            repository.save_rejected(rejection_id, local)
            record_rejection(
                rejection_id=rejection_id,
                paths=list(differing),
                device_id=device_id,
                workspace_id=workspace_id,
                workspace_root=repository.workspace_root,
            )
        held = {change.path for change in unsendable}
        adopted = tuple(sorted((set(hub_only) | set(differing)) - held))
        repository.move_to(remote)
        # A change held back by validation was never shareable, so the hub has
        # no version of it that supersedes anything -- and overwriting it would
        # throw away the edit the user was told to go and repair.
        repository.restore_from_index(list(adopted))
        # What only this machine had is still on disk and no longer tracked, so
        # the commit boundary picks it up again and it travels to the hub next.
        commit_shared_changes(repository, device_id=device_id)
    if repository.head() != remote:
        repository.push()
    return EnrollmentResult(
        workspace_id=workspace_id,
        mode="join",
        adopted=adopted,
        rejection_id=rejection_id,
        unsendable=unsendable,
    )


def _mode(repository: LocalSyncRepository, local: str, remote: str) -> PreviewMode:
    """Tell a first meeting apart from a reunion."""
    return "reconnect" if repository.merge_base(local, remote) is not None else "join"


def _classify(
    repository: LocalSyncRepository, local: str, remote: str
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Split the difference between the two sides into what each alone holds.

    Which comparison answers that depends on whether the two share history.
    Without a common commit, the trees themselves are the whole story: a file
    added on the way from this machine's tree to the hub's exists only there,
    a deleted one exists only here, and a modified one exists on both with
    different content.

    With a common commit, having the same file means nothing -- both sides
    inherited it. What separates them is who changed it since, which is the
    same question ordinary synchronization asks.
    """
    base = repository.merge_base(local, remote)
    if base is None:
        changes = repository.changed_paths(local, remote)
        return (
            tuple(sorted(path for path, status in changes.items() if status == "A")),
            tuple(sorted(path for path, status in changes.items() if status == "D")),
            tuple(sorted(path for path, status in changes.items() if status == "M")),
        )
    here = set(repository.changed_paths(base, local))
    there = set(repository.changed_paths(base, remote))
    return (
        tuple(sorted(there - here)),
        tuple(sorted(here - there)),
        tuple(sorted(here & there)),
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


@contextmanager
def _as_enrollment_error(action: str) -> Iterator[None]:
    """Report a Git failure as an enrollment failure.

    Reaching a hub fails for ordinary reasons -- it is off, this device's key
    is not registered there yet, the address is wrong -- and the layers above
    have to be able to say so. Letting Git's own exception through would make
    every one of those an unhandled error instead of an answer.
    """
    try:
        yield
    except (GitCommandError, SyncRepositoryError, OSError) as exc:
        raise EnrollmentError(f"{action}: {exc}") from exc


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
