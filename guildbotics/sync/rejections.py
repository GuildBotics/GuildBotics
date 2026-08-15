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
from datetime import UTC, datetime

from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.event_types import SYNC_UPDATE_REJECTED

#: Signature the sync manager depends on, so the recorder can be substituted.
RejectionRecorder = Callable[..., None]


def record_update_rejected(
    *,
    rejection_id: str,
    paths: Sequence[str],
    device_id: str,
    workspace_id: str,
) -> None:
    """Record one provider-neutral activity event for a rejected local change.

    Args:
        rejection_id (str): Identifies the ref holding the stashed commit on
            this device, and ties this event to it.
        paths (Sequence[str]): The ``.guildbotics``-relative paths not accepted.
        device_id (str): The device whose change was not accepted, which is the
            only device the manual recovery procedure can run on.
        workspace_id (str): The workspace the rejection happened in.
    """
    ActivityEventStore().record(
        {
            "type": SYNC_UPDATE_REJECTED,
            "workspace_id": workspace_id,
            "device_id": device_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "subject": rejection_id,
            "payload": {
                "rejection_id": rejection_id,
                "paths": list(paths),
                "source_device_id": device_id,
            },
        }
    )
