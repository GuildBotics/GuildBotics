"""Host entries that start a command.

A run's AI CLI turns share one isolated environment, and the host opens it
around the run: the command execution machinery
(:class:`~guildbotics.commands.runner.CommandRunner`) knows nothing of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cached_property
from pathlib import Path
from typing import Any
from uuid import uuid4

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.capabilities.workflow_completion_events import (
    record_workflow_completed,
    record_workflow_completion_missing,
)
from guildbotics.commands.errors import CommandError
from guildbotics.commands.models import CommandOutcome
from guildbotics.commands.runner import CommandRunner
from guildbotics.intelligences.agent_runtime.environment import command_environment
from guildbotics.intelligences.brains.cli_agent import get_cli_agent_mapping
from guildbotics.runtime.context import Context
from guildbotics.runtime.member_context import ensure_execution_subject, resolve_person
from guildbotics.runtime.workflow_invocation import (
    TICKET_WORKFLOW_COMMAND,
    WORKFLOW_INVOCATION_KEY,
    WorkflowInvocation,
    WorkflowSource,
)

__all__ = [
    "HostRunLedger",
    "prepare_command",
    "run_command",
    "run_in_environment",
    "run_main_command",
]


class HostRunLedger:
    """The host's run record, read and reported to by completion-managed turns.

    Where the record lives is settled once, from this host's own workspace, so
    nothing a turn passes decides which record the host reads. It is settled
    on first use: a command that never drives such a turn needs no workspace.
    """

    @cached_property
    def _store(self) -> RunStore:
        return RunStore()

    def require_completion(self, run_id: str) -> None:
        """Raise unless the run has recorded a terminal completion."""
        self._store.status(run_id)

    def evidence(self, run_id: str) -> list[dict[str, Any]]:
        """Return the evidence the run has recorded so far."""
        return self._store.evidence(run_id)

    def record_completed(self, run_id: str, attempt: int) -> None:
        """Record that the run's completion was found after an attempt."""
        record_workflow_completed(run_id=run_id, attempt=attempt)

    def record_completion_missing(
        self, run_id: str, attempt: int, max_attempts: int, error: str
    ) -> None:
        """Record an attempt that ended without the run's completion."""
        record_workflow_completion_missing(
            run_id=run_id, attempt=attempt, max_attempts=max_attempts, error=error
        )


def prepare_command(
    base_context: Context,
    command_name: str,
    command_args: Sequence[str],
    person_identifier: str | None = None,
    cwd: Path | None = None,
) -> CommandRunner:
    """Resolve a command for the member it runs as, once.

    The runner is the single read of the command: its file and what it
    declares. Whatever a host decides about the run (the lease, a slot) is
    decided on the runner it then starts, never on another reading.

    Args:
        base_context: Base runtime context.
        command_name: Command to run.
        command_args: Positional arguments for the command.
        person_identifier: Member to run as, or ``None`` for the default.
        cwd: Working directory for the command.

    Raises:
        CommandError: If the member cannot run commands or the command cannot
            be resolved.
    """
    person = ensure_execution_subject(
        resolve_person(base_context.team, person_identifier, allow_default=True)
    )
    return CommandRunner(
        base_context.clone_for(person),
        command_name,
        command_args,
        cwd,
        ledger=HostRunLedger(),
    )


async def run_command(
    base_context: Context,
    command_name: str,
    command_args: Sequence[str],
    person_identifier: str | None = None,
    cwd: Path | None = None,
) -> CommandOutcome:
    """Execute a command within the given context.

    A command that declares itself read-only takes no execution lease: its
    turns can change nothing, so it runs while the member is busy.
    """
    from guildbotics.runtime.person_lease import (
        PersonExecutionLease,
        PersonLeaseUnavailableError,
        current_person_lease,
    )

    runner = prepare_command(
        base_context, command_name, command_args, person_identifier, cwd
    )
    person_id = runner.context.person.person_id
    owned_lease = None
    try:
        inherited_lease = current_person_lease()
        if inherited_lease is not None and inherited_lease.person_id != person_id:
            raise RuntimeError("The active execution lease belongs to another person.")
        if inherited_lease is None and not runner.access.read_only:
            lease = PersonExecutionLease(person_id)
            try:
                lease.acquire(
                    source="manual", command=command_name, work_id=uuid4().hex
                )
            except PersonLeaseUnavailableError as exc:
                raise CommandError(str(exc)) from exc
            owned_lease = lease
        return await run_main_command(runner, source="manual")
    finally:
        try:
            await runner.context.aclose()
        finally:
            if owned_lease is not None:
                owned_lease.release()


async def run_main_command(
    runner: CommandRunner, *, source: WorkflowSource
) -> CommandOutcome:
    """Run a top-level command from a host entry.

    The ticket workflow runs only for a ticket the host selected, so it goes
    through the ticket selector, which settles the ticket around the run.

    Args:
        runner: The command to run, resolved for the member it runs as.
        source: Route that started the command.

    Returns:
        The command's outcome; for the ticket workflow, the rate-limit notice
        posted on the ticket instead, or nothing when there was no ticket.
    """
    if runner.command_name != TICKET_WORKFLOW_COMMAND:
        return await run_in_environment(runner)
    from guildbotics.drivers.ticket_selector import TicketSelector

    context = runner.context

    async def _run(invocation: WorkflowInvocation) -> CommandOutcome:
        context.shared_state[WORKFLOW_INVOCATION_KEY] = invocation
        return await run_in_environment(runner)

    selector = TicketSelector(context, source=source)
    outcome = await selector.run_next(context.person, _run)
    if isinstance(outcome, CommandOutcome):
        return outcome
    # No ticket to work on, or the rate-limit notice posted on it instead.
    return CommandOutcome(result=outcome, text_output=outcome or "")


async def run_in_environment(runner: CommandRunner) -> CommandOutcome:
    """Run a command in the isolated environment its AI CLI turns share.

    It is shaped for every AI CLI tool the member is configured with, and
    discarded when the run ends, however it ends. A command run inside
    another one shares that one's.

    Args:
        runner: The command to run, resolved for the member it runs as.

    Returns:
        The command's outcome.

    Raises:
        CommandError: If the member's AI CLI tool settings or the settings the
            environment's access contract is read from are invalid.
    """
    try:
        mapping = get_cli_agent_mapping(runner.context.person.person_id)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    tools = frozenset(info.adapter for info in mapping.values())
    async with command_environment(runner.access, tools):
        return await runner.run()
