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
from pathlib import Path

from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.diagnostics_events import record_correlated_log
from guildbotics.observability.event_types import SYNC_UPDATE_REJECTED
from guildbotics.utils.fileio import get_workspace_state_path
from guildbotics.utils.timestamps import utc_now_iso

#: Signature the sync manager depends on, so the recorder can be substituted.
RejectionRecorder = Callable[..., None]


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
    ActivityEventStore(
        get_workspace_state_path("events", workspace_root=workspace_root),
        workspace_root=workspace_root,
    ).record(
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
