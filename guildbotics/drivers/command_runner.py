"""Host entries that start a command.

The host reads a command once (:func:`prepare_command`): its file and what it
declares. It decides everything about the run from that one reading -- the
lease, a slot, whether the ticket selector runs it -- and hands the resolved
file to the command's isolated environment, which runs it and its
subcommands (:func:`run_in_environment`). Nothing of the command runs on the
host.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.capabilities.workflow_completion_events import (
    record_workflow_completed,
    record_workflow_completion_missing,
)
from guildbotics.commands.discovery import resolve_named_command
from guildbotics.commands.errors import CommandError, CommandFailedError
from guildbotics.commands.metadata import (
    command_access,
)
from guildbotics.commands.models import CommandOutcome
from guildbotics.drivers.member_context import ensure_execution_subject, resolve_person
from guildbotics.environment.command_environment import (
    command_environment,
    command_path,
)
from guildbotics.environment.contract import (
    AccessContractError,
    exchange_dir,
    protected_paths,
    validate_mount_source,
)
from guildbotics.environment.host_window import HostWindow
from guildbotics.environment.spec import guest_path
from guildbotics.intelligences.agent_runtime.models import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)
from guildbotics.intelligences.agent_runtime.wire import (
    CommandAccess,
    CommandFailure,
    CommandReply,
    CommandRequest,
)
from guildbotics.intelligences.cli_agents import (
    get_cli_agent_mapping,
)
from guildbotics.runtime.context import Context
from guildbotics.runtime.member_invocation import Work
from guildbotics.runtime.workflow_invocation import (
    TICKET_WORKFLOW_COMMAND,
    WORKFLOW_INVOCATION_KEY,
    WorkflowInvocation,
    WorkflowSource,
)
from guildbotics.utils.correlation import require_trace
from guildbotics.utils.fileio import get_member_clone_path, get_workspace_root
from guildbotics.utils.keep_awake import keep_awake
from guildbotics.utils.safe_paths import normalize_host_path

__all__ = [
    "HostRunLedger",
    "PreparedCommand",
    "host_command_cwd",
    "prepare_command",
    "prepare_host_command",
    "run_in_environment",
    "run_ledger",
    "run_main_command",
]


@dataclass(frozen=True)
class PreparedCommand:
    """A command read once for the member it runs as.

    ``context`` is the member's; its ``pipe`` is the command's input, and a
    workflow run's invocation is in its ``shared_state``. ``path`` is the
    command's file, and ``access`` what it declares. ``result_type`` is what
    the caller reads the main command's own result as, if it reads one.
    ``inputs`` is the directory of the files the Desktop handed over for the
    command, which its microVM reads. ``work`` is the work the caller runs it
    for, when it names one; a workflow run's is its invocation's.
    """

    context: Context
    command_name: str
    args: list[str]
    cwd: Path
    path: Path
    access: CommandAccess
    result_type: type[BaseModel] | None = None
    inputs: Path | None = None
    work: Work | None = None


class HostRunLedger:
    """The host's record of one command run, read and reported to by its
    completion-managed turns (a ``RunLedger``).

    Which run it is and where its record lives are settled by this host, so
    nothing a turn passes decides which record the host reads. The store is
    opened on first use: a command that never drives such a turn needs no
    workspace.
    """

    def __init__(self, run_id: str, work: Work) -> None:
        self.run_id = run_id
        self.work = work

    @property
    def work_kind(self) -> str:
        """The kind of work the run does."""
        return self.work.kind

    @cached_property
    def _store(self) -> RunStore:
        return RunStore()

    def require_completion(self) -> None:
        """Raise unless the run has recorded a terminal completion."""
        self._store.status(self.run_id)

    def evidence(self) -> list[dict[str, Any]]:
        """Return the evidence the run has recorded so far."""
        return self._store.evidence(self.run_id)

    def record_completed(self, attempt: int) -> None:
        """Record that the run's completion was found after an attempt."""
        record_workflow_completed(run_id=self.run_id, attempt=attempt)

    def record_completion_missing(
        self, attempt: int, max_attempts: int, error: str
    ) -> None:
        """Record an attempt that ended without the run's completion."""
        record_workflow_completion_missing(
            run_id=self.run_id,
            attempt=attempt,
            max_attempts=max_attempts,
            error=error,
        )


def run_ledger(command: PreparedCommand) -> HostRunLedger:
    """The record of the run ``command`` starts, and the work it does: its
    workflow run's, as the host selected it; else the run its trace is, doing
    the work the caller named or else manual work.

    Raises:
        RuntimeError: If no trace is open: every host entry opens the run's
            trace before it starts one.
    """
    invocation: WorkflowInvocation | None = command.context.shared_state.get(
        WORKFLOW_INVOCATION_KEY
    )
    run_id = (
        invocation.run_id if invocation is not None else ""
    ) or require_trace().trace_id
    work = (
        (invocation.work if invocation is not None else None)
        or command.work
        or Work("manual", run_id)
    )
    return HostRunLedger(run_id, work)


def host_command_cwd() -> Path:
    """Where a command the host starts on its own runs, and a Desktop run
    that names no directory: the exchange directory, the same however the
    host was started, so what it produces lands where the user looks for it.

    Raises:
        CommandError: If this process may not create it (on macOS, until the
            app is allowed the Documents folder), in the words the isolated
            environment uses for the same refusal.
    """
    cwd = exchange_dir()
    try:
        cwd = validate_mount_source(cwd, protected_paths(), grant=True, create=True)
    except AccessContractError as exc:
        raise CommandError(str(exc)) from exc
    return cwd


def prepare_host_command(context: Context, command: str) -> PreparedCommand:
    """Read a command line the host starts on its own (a scheduled or
    routine command, a dispatched workflow) for the member ``context`` runs
    as; it works in :func:`host_command_cwd`.

    Raises:
        ValueError: If ``command`` names no command.
        CommandError: If the command cannot be resolved.
    """
    words = shlex.split(command)
    if not words:
        raise ValueError(f"Empty or whitespace command string: {command!r}")
    return _prepared(context, words[0], words[1:], host_command_cwd())


def prepare_command(
    base_context: Context,
    command_name: str,
    command_args: Sequence[str],
    person_identifier: str | None,
    cwd: Path,
) -> PreparedCommand:
    """Read a command for the member it runs as, once.

    This is the single read of the command: its file and what it declares.
    Whatever a host decides about the run (the lease, a slot) is decided on
    what it returns, which is what the command's environment then runs,
    never on another reading.

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
    return _prepared(base_context.clone_for(person), command_name, command_args, cwd)


def _prepared(
    context: Context, command_name: str, command_args: Sequence[str], cwd: Path
) -> PreparedCommand:
    path = resolve_named_command(context, command_name)
    return PreparedCommand(
        context,
        command_name,
        [str(arg) for arg in command_args],
        normalize_host_path(cwd),
        path,
        command_access(path),
    )


async def run_main_command(
    command: PreparedCommand, *, source: WorkflowSource
) -> CommandOutcome:
    """Run a top-level command from a host entry.

    The ticket workflow runs only for a ticket the host selected, so it goes
    through the ticket selector, which settles the ticket around the run.

    Args:
        command: The command to run, read for the member it runs as.
        source: Route that started the command.

    Returns:
        The command's outcome; for the ticket workflow, the rate-limit notice
        posted on the ticket instead, or nothing when there was no ticket.
    """
    if command.command_name != TICKET_WORKFLOW_COMMAND:
        return await run_in_environment(command)
    from guildbotics.drivers.ticket_selector import TicketSelector

    context = command.context

    async def _run(invocation: WorkflowInvocation) -> CommandOutcome:
        context.shared_state[WORKFLOW_INVOCATION_KEY] = invocation
        return await run_in_environment(command)

    selector = TicketSelector(context, source=source)
    outcome = await selector.run_next(context.person, _run)
    if isinstance(outcome, CommandOutcome):
        return outcome
    # No ticket to work on, or the rate-limit notice posted on it instead.
    return CommandOutcome(result=outcome, text_output=outcome or "")


async def run_in_environment(command: PreparedCommand) -> CommandOutcome:
    """Run a command in the isolated environment booted for it.

    It is shaped for every AI CLI tool the member is configured with, and
    discarded when the run ends, however it ends. The machine is kept out of
    idle sleep for as long as it runs, since sleep would stop its microVM.
    What its microVM asks of the host is answered under the run's grant: the
    member it runs as, the run it records to -- its workflow run's, or else
    the one its trace is -- and the work it does: its workflow run's, the one
    the caller named, or else the run's own manual work.
    What it returns is read as the host reads anything from it: its result
    only as the type the caller asked for.

    Args:
        command: The command to run, read for the member it runs as.

    Returns:
        The command's outcome.

    Raises:
        CommandError: If the member's AI CLI tool settings or the settings the
            environment's access contract is read from are invalid, if this
            device cannot run the environment, or if the command failed as a
            command.
        CommandFailedError: If the command failed otherwise in its
            environment.
        CliAgentExecutionError: If an AI CLI tool's failure ended it.
    """
    context = command.context
    person_id = context.person.person_id
    try:
        mapping = get_cli_agent_mapping(person_id)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    tools = frozenset(info.adapter for info in mapping.values())
    workspace_root = get_workspace_root()
    invocation: WorkflowInvocation | None = context.shared_state.get(
        WORKFLOW_INVOCATION_KEY
    )
    ledger = run_ledger(command)
    window = HostWindow(
        person_id,
        ledger.work,
        workspace_root=workspace_root,
        ledger=ledger,
    )
    request = CommandRequest(
        path=command_path(command.path),
        name=command.command_name,
        args=command.args,
        cwd=guest_path(command.cwd),
        pipe=context.pipe,
        invocation=invocation,
        wants_result=command.result_type is not None,
    )
    try:
        with keep_awake():
            async with command_environment(
                command.access,
                tools,
                cwd=command.cwd,
                workspace_root=workspace_root,
                clone=get_member_clone_path(person_id, workspace_root),
                inputs=command.inputs,
                host=window,
            ) as environment:
                reply = await environment.execute(request)
    finally:
        await window.close()
    return _outcome(command, reply)


def _outcome(command: PreparedCommand, reply: CommandReply) -> CommandOutcome:
    """How the command ended, as the host takes it: a failure rebuilt, and a
    result only as the type the caller reads it as.

    Raises:
        CommandError: If it failed as a command, or its result is not of the
            type the caller reads it as.
        CommandFailedError: If it failed otherwise.
        CliAgentExecutionError: If an AI CLI tool's failure ended it.
    """
    if reply.failure is not None:
        raise _failure(reply.failure)
    result: Any = None
    if command.result_type is not None:
        try:
            result = command.result_type.model_validate(reply.result)
        except ValidationError as exc:
            raise CommandError(
                f"Command '{command.command_name}' did not return a "
                f"{command.result_type.__name__}."
            ) from exc
    return CommandOutcome(result=result, text_output=reply.text_output)


_CLI_AGENT_RESULT = TypeAdapter(CliAgentExecutionResult)


def _failure(failure: CommandFailure) -> Exception:
    """The host's own exception for a command's failure in its environment,
    raised from the AI CLI tool's failure it came from, so what reads the
    chain (a rate limit, a refused login) reads it as it would have been. A
    tool's failure that is not one as the host reads it is no cause."""
    cause = None
    if failure.cli_agent_result is not None:
        with suppress(ValidationError):
            cause = CliAgentExecutionError(
                cli_agent=failure.cli_agent,
                result=_CLI_AGENT_RESULT.validate_python(failure.cli_agent_result),
                message=failure.cli_agent_message,
            )
    if cause is not None and failure.type == CliAgentExecutionError.__name__:
        return cause
    error = (
        CommandError(failure.message)
        if failure.command
        else CommandFailedError(failure.type, failure.message)
    )
    error.__cause__ = cause
    return error
