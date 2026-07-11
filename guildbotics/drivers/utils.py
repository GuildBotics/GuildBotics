from __future__ import annotations

import datetime
import shlex
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from guildbotics.drivers.command_runner import CommandRunner
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.runtime import Context


async def run_with_logging(
    context: Context,
    command_name: str,
    task_type: str,
    action: Callable[[], Awaitable[Any]],
) -> bool:
    """Run an async action with timing logging and standardized error logging."""
    try:
        start_time = datetime.datetime.now()
        person = context.person
        context.logger.info(
            f"Running {task_type} command '{command_name}' for person '{person.person_id}'..."
        )
        record_correlated_event(
            event_type="command.started",
            default_source=task_type,
            person_id=person.person_id,
            command=command_name,
            payload={"command": command_name, "person": person.person_id},
        )

        await action()

        end_time = datetime.datetime.now()
        duration = (end_time - start_time).total_seconds()
        context.logger.info(
            f"Finished running {task_type} command '{command_name}' for person "
            f"'{person.person_id}' in {duration:.2f}s"
        )
        record_correlated_event(
            event_type="command.finished",
            default_source=task_type,
            person_id=person.person_id,
            command=command_name,
            payload={"command": command_name, "person": person.person_id},
        )
        return True
    except Exception as e:
        context.logger.error(
            f"Error running {task_type} command '{command_name}' for person "
            f"'{person.person_id}': {e}"
        )
        context.logger.error(traceback.format_exc())
        record_correlated_event(
            event_type="command.failed",
            default_source=task_type,
            person_id=person.person_id,
            command=command_name,
            payload={
                "command": command_name,
                "person": person.person_id,
                "error_type": type(e).__name__,
            },
        )
        return False


async def run_command(context: Context, command: str, task_type: str) -> bool:
    """Run a command within the given context and log its execution."""

    async def _action() -> None:
        words = shlex.split(command)
        if not words:
            raise ValueError(f"Empty or whitespace command string: {command!r}")
        await CommandRunner(context, words[0], words[1:]).run()

    return await run_with_logging(context, command, task_type, _action)
