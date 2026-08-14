"""Local command execution adapter."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from guildbotics.drivers.command_runner import run_command
from guildbotics.runtime.context import Context
from guildbotics.runtime.execution import (
    resolve_execution_placement,
)
from guildbotics.utils.env_loader import load_guildbotics_env


class LocalCommandExecutor:
    """Run a command on this machine through the existing CommandRunner."""

    async def run(
        self,
        context: Context,
        command_name: str,
        command_args: Sequence[str],
        person_identifier: str | None = None,
        cwd: Path | None = None,
        *,
        target_device: str | None = None,
    ) -> str:
        """Execute locally after resolving placement.

        Args:
            context: Base runtime context.
            command_name: Command to run.
            command_args: Positional arguments for the command.
            person_identifier: Optional member override.
            cwd: Working directory for the command process.
            target_device: Optional remote device id. This executor only
                accepts a local placement.
        """
        placement = resolve_execution_placement(target_device)
        if placement.kind != "local":
            raise RuntimeError(
                "Remote execution is not available. Run this command without "
                "a target device."
            )
        load_guildbotics_env(override=False)
        return await run_command(
            context,
            command_name=command_name,
            command_args=command_args,
            person_identifier=person_identifier,
            cwd=cwd,
        )
