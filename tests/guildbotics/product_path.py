"""Commands run as the host runs them, on this device.

For the tests that run on a real device (``GUILDBOTICS_CONTRACT_PROBE``, the
smoke tests): a command file the test writes runs in the microVM the host
boots for it, as the workspace's member it names, exactly as the host starts
any command -- only the command is the test's own file rather than one the
host resolves by name.
"""

from __future__ import annotations

from pathlib import Path

from guildbotics.commands.metadata import (
    command_access,
)
from guildbotics.commands.models import CommandOutcome
from guildbotics.drivers.command_runner import PreparedCommand, run_in_environment
from guildbotics.drivers.context import create_context
from guildbotics.drivers.member_context import ensure_execution_subject, resolve_person
from guildbotics.intelligences.agent_runtime.wire import (
    CommandAccess,
)


def workspace_member(person_id: str | None = None) -> str:
    """The workspace's member a command runs as: ``person_id``, or the
    team's default."""
    team = create_context().team
    person = resolve_person(team, person_id, allow_default=True)
    return ensure_execution_subject(person).person_id


async def run_file(
    path: Path,
    message: str = "",
    *,
    cwd: Path | None = None,
    person_id: str | None = None,
    access: CommandAccess | None = None,
) -> CommandOutcome:
    """Run the command ``path`` with ``message`` as its input, as the host
    runs a command it read: in the microVM booted for it.

    Args:
        path: The command's file; the microVM must hold it where the command
            works, or where it is granted.
        message: Its input.
        cwd: Where it works; its file's directory by default.
        person_id: The member it runs as; the team's default by default.
        access: What it declares, instead of what its file does.
    """
    context = create_context(message)
    member = next(
        m for m in context.team.members if m.person_id == workspace_member(person_id)
    )
    command = PreparedCommand(
        context.clone_for(member),
        path.stem,
        [],
        cwd or path.parent,
        path,
        access or command_access(path),
    )
    try:
        return await run_in_environment(command)
    finally:
        await command.context.aclose()
