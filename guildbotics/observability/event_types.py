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
