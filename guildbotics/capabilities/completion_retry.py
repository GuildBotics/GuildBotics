from __future__ import annotations

import asyncio

#: What ends a command run without the run being defective: stopping the
#: service cancels the work it is draining, and Ctrl-C ends an interactive
#: member command. Both are ``BaseException``, so a boundary that only catches
#: ``Exception`` never records the end of the run it started.
CANCELLATION_ERRORS = (asyncio.CancelledError, KeyboardInterrupt)


class CompletionRetryExhausted(Exception):
    """Raised when the agent never recorded a terminal completion in the budget."""

    def __init__(self, attempts: int, last_error: Exception) -> None:
        super().__init__(
            f"Agent did not complete after {attempts} attempt(s): {last_error}"
        )
        self.attempts = attempts
        self.last_error = last_error


def find_cli_agent_execution_error(
    exc: BaseException, *, category: str = ""
) -> BaseException | None:
    """Find a CliAgentExecutionError through common wrapper exception chains."""
    from guildbotics.intelligences.brains.cli_agent import CliAgentExecutionError

    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        obj_id = id(current)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        if isinstance(current, CliAgentExecutionError) and (
            not category or current.category == category
        ):
            return current
        last_error = getattr(current, "last_error", None)
        if isinstance(last_error, BaseException):
            stack.append(last_error)
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if current.__context__ is not None:
            stack.append(current.__context__)
    return None


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
