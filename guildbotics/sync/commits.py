"""The commit boundary: which local changes may become shared history.

One implementation serves the two moments a device turns its own files into
commits -- the ongoing queue, and the one-off commit a device makes of its
existing content when it joins a hub. Both look at the working tree rather than
at what was announced, so an edit made with an external editor and a change
whose save notification was lost are picked up the same way.

A file that does not validate is not an error to report and forget: it is the
user's work, held back until it can be shared, and never overwritten by the
hub's version in the meantime.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from guildbotics.sync.local_repository import LocalSyncRepository, WorkingTreeChange
from guildbotics.utils.timestamps import utc_now_iso
from guildbotics.workspace.validation import (
    SharedFileInvalidError,
    validate_shared_file,
)


@dataclass(frozen=True)
class UnsendableChange:
    """A local change held back because the file does not validate.

    Attributes:
        path (str): The path relative to ``.guildbotics/``.
        reason (str): Why it was held back, for the user to act on.
    """

    path: str
    reason: str


@dataclass(frozen=True)
class CommitOutcome:
    """What one pass over the working tree produced.

    Attributes:
        head (str | None): The commit the branch is on afterwards, or None when
            the repository still has none.
        unsendable (tuple[UnsendableChange, ...]): Changes left in the working
            tree because they do not validate.
    """

    head: str | None
    unsendable: tuple[UnsendableChange, ...]


def commit_shared_changes(
    repository: LocalSyncRepository, *, device_id: str
) -> CommitOutcome:
    """Commit every shared change that validates, holding back the rest."""
    sendable: list[WorkingTreeChange] = []
    held: list[UnsendableChange] = []
    for change in repository.working_tree_changes():
        if change.deleted:
            sendable.append(change)
            continue
        try:
            validate_shared_file(change.path, repository.read_working_tree(change.path))
        except SharedFileInvalidError as exc:
            held.append(UnsendableChange(path=change.path, reason=exc.reason))
        else:
            sendable.append(change)
    if sendable:
        repository.stage([change.path for change in sendable])
        repository.commit(_commit_message(sendable, device_id))
    return CommitOutcome(head=repository.head(), unsendable=tuple(held))


def validate_received(
    repository: LocalSyncRepository, revision: str, paths: Iterable[str]
) -> None:
    """Check arriving files with the same validation the send side used.

    Only validated content is ever sent, so a file that fails here means a
    defect or a damaged repository -- never a mistake the user made. What that
    means differs by caller, so the failure is reported as-is: the queue stops
    on it, while a workspace being joined refuses the hub instead.

    Raises:
        SharedFileInvalidError: When an arriving file fails the boundary.
    """
    for path in paths:
        data = repository.read_blob(revision, path)
        if data is None:
            continue
        validate_shared_file(path, data)


def _commit_message(changes: Sequence[WorkingTreeChange], device_id: str) -> str:
    deleted = sum(1 for change in changes if change.deleted)
    return (
        f"Sync shared state: {len(changes) - deleted} written, {deleted} deleted"
        f"\n\nDevice: {device_id}\nRecorded-At: {utc_now_iso()}\n"
    )
