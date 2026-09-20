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

#: The PR / issue a member command worked on (or read), recorded by the member
#: CLI so the trace it runs inside can name that item as its work target. It is
#: diagnostics only: the domain events above carry the activity itself.
GITHUB_WORK_TARGET_EVENT_TYPE = "github.work_target"

#: Events that mean "the execution this trace covers is over, and it ended
#: well". Only the layer that opened the trace records one of these, so they
#: are the sole evidence that can make a trace read as success. A child
#: ``span.finished`` reports that a single provider call returned and never
#: stands in for them. Failure needs no catalog: any ``*.failed`` event fails
#: the trace whichever layer records it.
TRACE_COMPLETED_EVENT_TYPES = frozenset(
    {
        "command.finished",
        "member.command.finished",
        "system.finished",
        "diagnostics.completed",
        "verify.completed",
    }
)
