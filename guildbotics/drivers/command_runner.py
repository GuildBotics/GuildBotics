from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from guildbotics.commands.discovery import resolve_named_command
from guildbotics.commands.errors import (
    CommandError,
    PersonExecutionNotAllowedError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
)
from guildbotics.commands.models import CommandOutcome, CommandSpec
from guildbotics.commands.spec_factory import CommandSpecFactory
from guildbotics.entities.team import Person
from guildbotics.runtime.context import Context
from guildbotics.runtime.member_context import resolve_person

__all__ = [
    "CommandRunner",
    "PersonExecutionNotAllowedError",
    "PersonNotFoundError",
    "PersonSelectionRequiredError",
    "_resolve_person",
    "run_command",
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
        self._context = context
        self._command_name = command_name
        self._command_args = list(command_args)
        self._registry: dict[str, CommandSpec] = {}
        self._call_stack: list[str] = []
        self._cwd = cwd if cwd is not None else Path.cwd()
        self._spec_factory = CommandSpecFactory(context)
        self._main_spec = self._prepare_main_spec()

    async def run(self) -> str:
        await self._run_with_children(self._main_spec)
        return self._context.pipe

    def _prepare_main_spec(self) -> CommandSpec:
        path = resolve_named_command(self._context, self._command_name)
        spec = self._spec_factory.prepare_main_spec(
            path, self._command_name, self._command_args, self._cwd
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
            command = spec.command_class(self._context, spec, spec.cwd)
            outcome = await command.run()
            if outcome is not None:
                self._context.update(
                    command.options.output_key, outcome.result, outcome.text_output
                )
            return outcome
        finally:
            self._call_stack.pop()

    async def _invoke(self, name: str, *args: Any, **kwargs: Any) -> Any:
        cwd = kwargs.pop("cwd", None)
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
) -> str:
    """Execute a command within the given context."""
    person = resolve_person(
        base_context.team.members,
        person_identifier,
        default_to_single_active=True,
    )
    if person.person_type == "human":
        raise PersonExecutionNotAllowedError(person.person_id)
    context = base_context.clone_for(person)
    runner = CommandRunner(context, command_name, command_args, cwd)
    return await runner.run()


def _resolve_person(members: Sequence[Person], identifier: str | None) -> Person:
    return resolve_person(members, identifier, default_to_single_active=True)
