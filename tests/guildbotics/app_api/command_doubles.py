"""A resolved command standing in for the one the App API prepares and runs.

The App API resolves a command once (``prepare_command``) and runs that very
runner (``run_main_command``). These doubles replace both, so a test observes
what the API resolved and decides what running it does.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from guildbotics.app_api import runtime as runtime_module
from guildbotics.commands.metadata import CommandAccess
from guildbotics.commands.models import CommandOutcome


@dataclass
class RunContext:
    person: Any
    pipe: str = ""
    closed: bool = False

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class PreparedCommand:
    """What the API resolved: the command, for whom, where, and its input."""

    base_context: Any
    command_name: str
    args: list[str]
    cwd: Path | None
    access: CommandAccess
    context: RunContext = field(repr=False)


def stub_commands(
    monkeypatch: pytest.MonkeyPatch,
    run: Callable[[PreparedCommand], Awaitable[CommandOutcome]],
    *,
    access: CommandAccess | None = None,
) -> list[PreparedCommand]:
    """Resolve every command the API runs to a double that ``run`` runs.

    Returns:
        The commands the API prepared, in order.
    """
    prepared: list[PreparedCommand] = []

    def prepare(
        base_context: Any,
        command_name: str,
        command_args: Sequence[str],
        person_identifier: str | None = None,
        cwd: Path | None = None,
    ) -> PreparedCommand:
        member = next(
            member
            for member in base_context.team.members
            if member.person_id == person_identifier
        )
        command = PreparedCommand(
            base_context=base_context,
            command_name=command_name,
            args=list(command_args),
            cwd=cwd,
            access=access or CommandAccess(),
            context=RunContext(person=member),
        )
        prepared.append(command)
        return command

    async def run_main_command(
        runner: PreparedCommand, *, source: str
    ) -> CommandOutcome:
        assert source == "manual"
        return await run(runner)

    monkeypatch.setattr(runtime_module, "prepare_command", prepare)
    monkeypatch.setattr(runtime_module, "run_main_command", run_main_command)
    return prepared
