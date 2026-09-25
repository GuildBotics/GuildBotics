from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from guildbotics.commands.discovery import resolve_named_command
from guildbotics.commands.errors import (
    CommandError,
    PersonExecutionNotAllowedError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
)
from guildbotics.commands.metadata import command_access
from guildbotics.commands.models import CommandOutcome, CommandSpec
from guildbotics.commands.spec_factory import CommandSpecFactory
from guildbotics.intelligences.agent_runtime.environment import command_environment
from guildbotics.runtime.context import Context
from guildbotics.runtime.member_context import ensure_execution_subject, resolve_person
from guildbotics.runtime.workflow_invocation import (
    TICKET_WORKFLOW_COMMAND,
    WORKFLOW_INVOCATION_KEY,
    WorkflowInvocation,
    WorkflowSource,
)

__all__ = [
    "CommandRunner",
    "PersonExecutionNotAllowedError",
    "PersonNotFoundError",
    "PersonSelectionRequiredError",
    "run_command",
    "run_main_command",
]


class CommandRunner:
    """Coordinate the execution of main and sub commands."""

    def __init__(
        self,
        context: Context,
        command_name: str,
        command_args: Sequence[str],
        cwd: Path | None = None,
    ) -> None:
        context.set_invoker(self._invoke)
        self.context = context
        self.command_name = command_name
        self._command_args = list(command_args)
        self._registry: dict[str, CommandSpec] = {}
        self._call_stack: list[str] = []
        self._cwd = cwd if cwd is not None else Path.cwd()
        self._spec_factory = CommandSpecFactory(context)
        self._main_spec = self._prepare_main_spec()
        assert self._main_spec.path is not None
        #: What the main command declares of its turns' access; every turn of
        #: the run, its subcommands' included, is held to it.
        self.access = command_access(self._main_spec.path)

    async def run(self) -> CommandOutcome:
        """Run the command and return the main command's result.

        Returns:
            The main command's own result (``None`` when it produced none) and
            the run's text output, ``Context.pipe``.
        """
        # The command's AI CLI turns share one microVM, discarded with the run.
        async with command_environment(self.access):
            outcome = await self._run_with_children(self._main_spec)
        return CommandOutcome(
            result=outcome.result if outcome is not None else None,
            text_output=self.context.pipe,
        )

    def _prepare_main_spec(self) -> CommandSpec:
        path = resolve_named_command(self.context, self.command_name)
        spec = self._spec_factory.prepare_main_spec(
            path, self.command_name, self._command_args, self._cwd
        )
        return spec

    async def _run_with_children(
        self, spec: CommandSpec, parent: CommandSpec | None = None
    ) -> CommandOutcome | None:
        self._registry[spec.name] = spec
        spec.command_class.populate_spec(
            spec, self._spec_factory, parent.class_resolver if parent else None
        )

        # Run child commands first
        for child in spec.children:
            await self._run_with_children(child, spec)

        # Run this command
        outcome = await self._run(spec)
        return outcome

    async def _run(self, spec: CommandSpec) -> CommandOutcome | None:
        name = spec.name
        if name in self._call_stack:
            cycle = " -> ".join([*self._call_stack, name])
            raise CommandError(f"Cyclic command invocation detected: {cycle}")

        self._call_stack.append(name)

        try:
            command = spec.command_class(self.context, spec, spec.cwd)
            outcome = await command.run()
            if outcome is not None:
                self.context.update(
                    command.options.output_key, outcome.result, outcome.text_output
                )
            return outcome
        finally:
            self._call_stack.pop()

    async def _invoke(self, name: str, *args: Any, **kwargs: Any) -> Any:
        cwd = kwargs.pop("cwd", None)
        execution_context = kwargs.get("agent_execution_context")
        if isinstance(execution_context, dict) and execution_context.get(
            "max_completion_attempts"
        ):
            from guildbotics.drivers.agent_turn import run_agent_turn

            async def _invoke_turn(
                turn_context: dict[str, Any], parameters: dict[str, str]
            ) -> Any:
                return await self._invoke_once(
                    name,
                    args,
                    {
                        **kwargs,
                        **parameters,
                        "agent_execution_context": turn_context,
                    },
                    cwd,
                )

            return await run_agent_turn(
                invoke=_invoke_turn,
                execution_context=execution_context,
            )
        return await self._invoke_once(name, args, kwargs, cwd)

    async def _invoke_once(
        self,
        name: str,
        args: Sequence[Any],
        kwargs: dict[str, Any],
        cwd: Path | None,
    ) -> Any:
        spec = self._spec_factory.build_from_entry(
            self._current_spec(),
            {
                "name": name,
                "args": list(args),
                "params": kwargs,
                "cwd": cwd,
            },
        )
        outcome = await self._run_with_children(spec)
        return outcome.result if outcome else None

    def _current_spec(self) -> CommandSpec:
        if self._call_stack:
            current_name = self._call_stack[-1]
            current_spec = self._registry.get(current_name)
            if current_spec is not None:
                return current_spec
        return self._main_spec


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
    person = ensure_execution_subject(
        resolve_person(base_context.team, person_identifier, allow_default=True)
    )
    from guildbotics.runtime.person_lease import (
        PersonExecutionLease,
        PersonLeaseUnavailableError,
        current_person_lease,
    )

    inherited_lease = current_person_lease()
    if inherited_lease is not None and inherited_lease.person_id != person.person_id:
        raise RuntimeError("The active execution lease belongs to another person.")
    context = base_context.clone_for(person)
    owned_lease = None
    try:
        # The declaration is read once: the lease is decided on the very
        # command that runs.
        runner = CommandRunner(context, command_name, command_args, cwd)
        if inherited_lease is None and not runner.access.read_only:
            lease = PersonExecutionLease(person.person_id)
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
            await context.aclose()
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
        return await runner.run()
    from guildbotics.drivers.ticket_selector import TicketSelector

    context = runner.context

    async def _run(invocation: WorkflowInvocation) -> CommandOutcome:
        context.shared_state[WORKFLOW_INVOCATION_KEY] = invocation
        return await runner.run()

    selector = TicketSelector(context, source=source)
    outcome = await selector.run_next(context.person, _run)
    if isinstance(outcome, CommandOutcome):
        return outcome
    # No ticket to work on, or the rate-limit notice posted on it instead.
    return CommandOutcome(result=outcome, text_output=outcome or "")
