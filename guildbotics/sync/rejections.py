"""Recording the fact that the hub did not accept a local change.

A rejection is normal operation, not an error: the hub accepted another
device's change to the same file first, and this device's commit is stashed
under a rejected ref instead of being merged. The user is never asked to
resolve anything, so only the facts needed to *find* the stashed commit are
recorded -- the paths, the device that made the change, the time, and the
``rejection_id``. The stashed content itself stays out of activity history and
out of every API, because recovery is a manual, source-device-only procedure.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.diagnostics_events import record_correlated_log
from guildbotics.observability.event_types import SYNC_UPDATE_REJECTED
from guildbotics.sync.local_repository import RejectedChange
from guildbotics.utils.fileio import get_workspace_state_path
from guildbotics.utils.timestamps import utc_now_iso

#: Signature the sync manager depends on, so the recorder can be substituted.
RejectionRecorder = Callable[..., None]


@dataclass(frozen=True)
class RecordedRejection:
    """A rejected ref joined with the event recorded for the same workspace.

    The ref says the change is still held; the event supplies the paths that
    the commit itself cannot. Both must come from one workspace, or a retry
    that outlives a workspace switch would describe A's ref with B's files.
    """

    rejection_id: str
    occurred_at: str
    paths: tuple[str, ...]


def _event_store(workspace_root: Path) -> ActivityEventStore:
    return ActivityEventStore(
        get_workspace_state_path("events", workspace_root=workspace_root),
        workspace_root=workspace_root,
    )


def record_update_rejected(
    *,
    rejection_id: str,
    paths: Sequence[str],
    device_id: str,
    workspace_id: str,
    workspace_root: Path,
) -> None:
    """Record one provider-neutral activity event for a rejected local change.

    Args:
        rejection_id (str): Identifies the ref holding the stashed commit on
            this device, and ties this event to it.
        paths (Sequence[str]): The ``.guildbotics``-relative paths not accepted.
        device_id (str): The device whose change was not accepted, which is the
            only device the manual recovery procedure can run on.
        workspace_id (str): The workspace the rejection happened in.
        workspace_root (Path): The workspace whose repository holds the stashed
            commit. Passed explicitly so the event lands in the copy the
            ``rejection_id`` can actually be resolved against, rather than in
            whichever workspace happens to be selected.
    """
    # Also on this device's own diagnostics log. The activity event is shared,
    # so it describes something that happened on some machine; the log is where
    # a person looks to find out what happened on *this* one, and the ref being
    # discussed exists nowhere else.
    record_correlated_log(
        level="warning",
        message=(
            f"Update not applied: {len(paths)} file(s) set aside as {rejection_id} "
            f"({', '.join(paths)})"
        ),
    )
    _event_store(workspace_root).record(
        {
            "type": SYNC_UPDATE_REJECTED,
            "workspace_id": workspace_id,
            "device_id": device_id,
            "timestamp": utc_now_iso(),
            "subject": rejection_id,
            "payload": {
                "rejection_id": rejection_id,
                "paths": list(paths),
                "source_device_id": device_id,
            },
        }
    )


def recorded_rejections(workspace_root: Path) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Return ``rejection_id -> (time, paths)`` from one workspace's events.

    The workspace is an argument so a caller that already holds a repository
    does not resolve the selected workspace a second time and join this
    lookup with a different workspace's refs.
    """
    recorded: dict[str, tuple[str, tuple[str, ...]]] = {}
    for event in _event_store(workspace_root).list_between(
        datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC)
    ):
        if event.get("kind") != SYNC_UPDATE_REJECTED:
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        rejection_id = str(payload.get("rejection_id") or "")
        if not rejection_id:
            continue
        paths = payload.get("paths")
        recorded[rejection_id] = (
            str(event.get("occurred_at") or ""),
            tuple(str(path) for path in paths) if isinstance(paths, list) else (),
        )
    return recorded


def describe_rejected(
    held: Sequence[RejectedChange], workspace_root: Path
) -> tuple[RecordedRejection, ...]:
    """Join already-listed rejected refs with that workspace's recorded paths."""
    if not held:
        return ()
    recorded = recorded_rejections(workspace_root)
    return tuple(
        RecordedRejection(
            rejection_id=change.rejection_id,
            occurred_at=recorded.get(change.rejection_id, ("", ()))[0]
            or change.occurred_at,
            paths=recorded.get(change.rejection_id, ("", ()))[1],
        )
        for change in held
    )
