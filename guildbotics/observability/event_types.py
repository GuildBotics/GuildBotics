"""Event type catalogs shared across observability stores."""

from __future__ import annotations

# Start / finish / failure lifecycle of top-level and member CLI commands.
COMMAND_LIFECYCLE_EVENT_TYPES = frozenset(
    {
        "command.started",
        "command.finished",
        "command.failed",
        "member.command.started",
        "member.command.finished",
        "member.command.failed",
    }
)

#: A local change the hub did not accept because another device changed the
#: same file first. Provider-neutral by construction: it names the paths, the
#: device that made the change, and the ``rejection_id`` that locates the
#: stashed commit on that device. The stashed content itself is never recorded.
SYNC_UPDATE_REJECTED = "sync.update_rejected"
