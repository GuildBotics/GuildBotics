"""How a failed AI CLI command run is classified."""

from __future__ import annotations

import asyncio

from guildbotics.intelligences.common import find_cli_agent_execution_error

#: What ends a command run without the run being defective: stopping the
#: service cancels the work it is draining, and Ctrl-C ends an interactive
#: member command. Both are ``BaseException``, so a boundary that only catches
#: ``Exception`` never records the end of the run it started.
CANCELLATION_ERRORS = (asyncio.CancelledError, KeyboardInterrupt)


def command_failure_payload(exc: BaseException) -> dict[str, str]:
    """Return the error fields a failed command run records.

    Every layer that records ``command.failed`` classifies the failure the
    same way, because the ``code`` decides what the Desktop does with it:
    ``cancelled`` is the expected end of a stop and ``cli_agent_authentication``
    is resolved with a credential alert, so neither opens a generic execution
    alert (``system_alerts._IGNORED_COMMAND_FAILURES``). Keeping the
    classification here means a recording site cannot silently disagree with
    the alert rules.

    Args:
        exc: The exception that ended the command run.

    Returns:
        The ``error_type`` and ``code`` fields for the event payload.
    """
    if isinstance(exc, CANCELLATION_ERRORS):
        code = "cancelled"
    elif find_cli_agent_execution_error(exc, category="authentication"):
        code = "cli_agent_authentication"
    else:
        code = ""
    return {"error_type": type(exc).__name__, "code": code}
