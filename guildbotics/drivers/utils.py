from __future__ import annotations

import contextlib
import datetime
import shlex
import traceback
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from guildbotics.capabilities.completion_retry import command_failure_payload
from guildbotics.drivers.command_runner import CommandRunner
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.runtime import Context


@contextlib.contextmanager
def command_boundary(
    *, command_name: str, task_type: str, person_id: str
) -> Iterator[None]:
    """Record the trace boundary events around one command run.

    Only the layer that opened the trace can say the whole execution started
    and ended, so every route that opens one records these. A provider's
    ``span.finished`` reports that a single call returned and never stands in
    for them; a chat workflow decides with an LLM call before its agent turn
    even starts.

    The failure is recorded and re-raised, so callers that drive their own
    retry or cleanup on the exception keep seeing it.

    Args:
        command_name: Command the trace is running.
        task_type: Source recorded when the trace does not name one.
        person_id: Member the run belongs to.
    """
    payload = {"command": command_name, "person": person_id}
    record_correlated_event(
        event_type="command.started",
        default_source=task_type,
        person_id=person_id,
        command=command_name,
        payload=payload,
    )
    try:
        yield
    except Exception as exc:
        record_correlated_event(
            event_type="command.failed",
            default_source=task_type,
            person_id=person_id,
            command=command_name,
            payload={**payload, **command_failure_payload(exc)},
        )
        raise
    record_correlated_event(
        event_type="command.finished",
        default_source=task_type,
        person_id=person_id,
        command=command_name,
        payload=payload,
    )


async def run_with_logging(
    context: Context,
    command_name: str,
    task_type: str,
    action: Callable[[], Awaitable[Any]],
) -> bool:
    """Run an async action with timing logging and standardized error logging."""
    person = context.person
    start_time = datetime.datetime.now()
    context.logger.info(
        f"Running {task_type} command '{command_name}' for person '{person.person_id}'..."
    )
    try:
        with command_boundary(
            command_name=command_name,
            task_type=task_type,
            person_id=person.person_id,
        ):
            await action()
    except Exception as e:
        context.logger.error(
            f"Error running {task_type} command '{command_name}' for person "
            f"'{person.person_id}': {e}"
        )
        context.logger.error(traceback.format_exc())
        return False
    duration = (datetime.datetime.now() - start_time).total_seconds()
    context.logger.info(
        f"Finished running {task_type} command '{command_name}' for person "
        f"'{person.person_id}' in {duration:.2f}s"
    )
    return True


async def run_command(context: Context, command: str, task_type: str) -> bool:
    """Run a command within the given context and log its execution."""

    async def _action() -> None:
        words = shlex.split(command)
        if not words:
            raise ValueError(f"Empty or whitespace command string: {command!r}")
        await CommandRunner(context, words[0], words[1:]).run()

    return await run_with_logging(context, command, task_type, _action)
