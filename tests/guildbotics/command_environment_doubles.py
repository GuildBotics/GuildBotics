"""A command's isolated environment, stood in for by the test's own process.

The host runs every command in a microVM booted for it, where the command
execution machinery runs it. A test that is about what a command does rather
than where it runs has the machinery run the very command the host read, in
this process, with the host's own context: :func:`commands_in_process`.
:func:`machinery` makes the machinery the way the environment's entry does,
for a command a test names. A test about what the host does around a
command it starts on its own has the command run as its own double does:
:func:`runs_as`.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic_core import to_jsonable_python

from guildbotics.commands.agent_turn import RunLedger
from guildbotics.commands.discovery import resolve_named_command
from guildbotics.commands.metadata import CommandAccess
from guildbotics.commands.models import CommandOutcome
from guildbotics.commands.runner import CommandRunner
from guildbotics.drivers import command_runner, utils, workflow_dispatcher
from guildbotics.drivers.command_runner import (
    PreparedCommand,
    host_command_cwd,
)
from guildbotics.intelligences.agent_runtime.host_client import CommandReply


def everywhere(cwd: Path) -> dict[str, bool]:
    """Mounts that let a command work anywhere on the drive ``cwd`` is on."""
    root = Path(cwd).as_posix().split("/", 1)[0]
    return {root or "/": True}


def machinery(
    context: Any,
    name: str,
    args: Sequence[Any] = (),
    cwd: Path | None = None,
    *,
    ledger: RunLedger | None = None,
) -> CommandRunner:
    """The machinery as the environment's entry makes it, for the command
    ``name`` resolved for ``context``'s member, working in ``cwd`` (the
    process's by default) where it may work anywhere."""
    where = cwd or Path.cwd()
    return CommandRunner(
        context,
        name,
        args,
        where,
        path=resolve_named_command(context, name),
        mounts=everywhere(where),
        ledger=ledger,
    )


@pytest.fixture
def commands_in_process(monkeypatch: pytest.MonkeyPatch) -> list[PreparedCommand]:
    """Run every command the host starts in this process instead of a
    microVM, with the host's context and run record; its result is read as
    the host reads one.

    Returns:
        The commands run, in order.
    """
    ran: list[PreparedCommand] = []

    async def run_in_environment(command: PreparedCommand) -> CommandOutcome:
        ran.append(command)
        runner = CommandRunner(
            command.context,
            command.command_name,
            command.args,
            command.cwd,
            path=command.path,
            mounts=everywhere(command.cwd),
            ledger=command_runner.run_ledger(command),
        )
        outcome = await runner.run()
        return command_runner._outcome(
            command,
            CommandReply(
                result=(
                    to_jsonable_python(outcome.result, fallback=str)
                    if command.result_type is not None
                    else None
                ),
                text_output=outcome.text_output,
            ),
        )

    monkeypatch.setattr(command_runner, "run_in_environment", run_in_environment)
    monkeypatch.setattr(workflow_dispatcher, "run_in_environment", run_in_environment)
    return ran


def runs_as(
    monkeypatch: pytest.MonkeyPatch, runner_class: Any
) -> list[PreparedCommand]:
    """Run every command the host starts on its own as ``runner_class`` does,
    made with the context, name, arguments and working directory the host
    read it with, and run; no command file is read for it, so the member may
    be a test's own.

    Returns:
        The commands run, in order.
    """
    ran: list[PreparedCommand] = []

    def prepare_host_command(context: Any, command: str) -> PreparedCommand:
        name, *args = shlex.split(command)
        return PreparedCommand(
            context, name, args, host_command_cwd(), Path(name), CommandAccess()
        )

    async def run_in_environment(command: PreparedCommand) -> Any:
        ran.append(command)
        runner = runner_class(
            command.context, command.command_name, command.args, command.cwd
        )
        return await runner.run()

    for module in (command_runner, utils, workflow_dispatcher):
        monkeypatch.setattr(module, "prepare_host_command", prepare_host_command)
    for module in (command_runner, workflow_dispatcher):
        monkeypatch.setattr(module, "run_in_environment", run_in_environment)
    return ran
