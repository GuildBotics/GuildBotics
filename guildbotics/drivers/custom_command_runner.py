from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from guildbotics.drivers.commands.command_base import CommandBase
from guildbotics.drivers.commands.discovery import resolve_named_command
from guildbotics.drivers.commands.errors import (
    CustomCommandError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
)
from guildbotics.drivers.commands.markdown_command import MarkdownCommand
from guildbotics.drivers.commands.models import CommandOutcome, CommandSpec
from guildbotics.drivers.commands.spec_factory import CommandSpecFactory
from guildbotics.entities.team import Person
from guildbotics.runtime.context import Context
from guildbotics.utils.fileio import load_markdown_with_frontmatter
from guildbotics.utils.import_utils import ClassResolver


class CustomCommandExecutor:
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
        await self._execute_with_children(self._main_spec)
        return self._context.pipe

    def _prepare_main_spec(self) -> CommandSpec:
        path = resolve_named_command(self._context, self._command_name)
        spec = self._spec_factory.prepare_main_spec(
            path, self._command_name, self._command_args, self._cwd
        )
        self._register(spec)
        return spec

    def _register(self, spec: CommandSpec) -> None:
        self._registry[spec.name] = spec

    def _ensure_spec_loaded(
        self, spec: CommandSpec, parent: CommandSpec | None = None
    ) -> None:
        if spec.command_class == MarkdownCommand:
            self._attach_markdown_metadata(spec, parent)

    def _attach_markdown_metadata(
        self, spec: CommandSpec, parent: CommandSpec | None = None
    ) -> None:
        if spec.path is None:
            return
        config = load_markdown_with_frontmatter(spec.path)
        spec.class_resolver = ClassResolver(
            config.get("schema", ""), parent.class_resolver if parent else None
        )
        spec.children = []

        raw_commands = config.get("commands")
        if raw_commands is None:
            entries: list[Any] = []
        elif isinstance(raw_commands, Sequence) and not isinstance(
            raw_commands, (str, bytes)
        ):
            entries = list(raw_commands)
        else:
            entries = [str(raw_commands)]

        for entry in entries:
            child = self._build_command_from_entry(entry, spec)
            spec.children.append(child)
            self._register(child)

    def _build_command_from_entry(self, entry: Any, anchor: CommandSpec) -> CommandSpec:
        return self._spec_factory.build_from_entry(anchor, entry)

    async def _execute_with_children(
        self, spec: CommandSpec, parent: CommandSpec | None = None
    ) -> CommandOutcome | None:
        self._ensure_spec_loaded(spec, parent)

        # Execute child commands first
        for child in spec.children:
            await self._execute_with_children(child, spec)

        # Execute the main command
        outcome = await self._execute_spec(spec)
        return outcome

    async def _execute_spec(self, spec: CommandSpec) -> CommandOutcome | None:
        name = spec.name
        if name in self._call_stack:
            cycle = " -> ".join(self._call_stack + [name])
            raise CustomCommandError(f"Cyclic command invocation detected: {cycle}")

        self._call_stack.append(name)

        try:
            command = self._build_command(spec)
            outcome = await command.run()
            if outcome is not None:
                self._context.update(
                    command.options.output_key, outcome.result, outcome.text_output
                )
            return outcome
        finally:
            self._call_stack.pop()

    def _build_command(self, spec: CommandSpec) -> CommandBase:
        return spec.command_class(self._context, spec, self._cwd)

    async def _invoke(self, name: str, *args: Any, **kwargs: Any) -> Any:
        anchor = self._current_spec()
        spec = self._create_dynamic_spec(anchor, name, *args, **kwargs)
        outcome = await self._execute_with_children(spec)
        return outcome.result if outcome else None

    def _create_dynamic_spec(
        self, anchor: CommandSpec, name: str, *args: Any, **kwargs: Any
    ) -> CommandSpec:
        data = {
            "name": name,
            "args": list(args),
            "params": kwargs,
        }
        spec = self._build_command_from_entry(data, anchor)
        self._register(spec)
        return spec

    def _current_spec(self) -> CommandSpec:
        if self._call_stack:
            current_name = self._call_stack[-1]
            current_spec = self._registry.get(current_name)
            if current_spec is not None:
                return current_spec
        return self._main_spec


async def run_custom_command(
    base_context: Context,
    command_name: str,
    command_args: Sequence[str],
    person_identifier: str | None = None,
    cwd: Path | None = None,
) -> str:
    """Execute a custom prompt command and return the rendered output."""
    person = _resolve_person(base_context.team.members, person_identifier)
    context = base_context.clone_for(person)
    executor = CustomCommandExecutor(context, command_name, command_args, cwd)
    return await executor.run()


def _resolve_person(members: Sequence[Person], identifier: str | None) -> Person:
    if identifier is None:
        active_members = [member for member in members if member.is_active]
        if len(active_members) == 1:
            return active_members[0]
        available = _list_person_labels(members)
        raise PersonSelectionRequiredError(available)

    person = _find_person(members, identifier)
    if person is None:
        available = _list_person_labels(members)
        raise PersonNotFoundError(identifier, available)
    return person


def _find_person(members: Sequence[Person], identifier: str) -> Person | None:
    lower_identifier = identifier.casefold()
    for member in members:
        if member.person_id.casefold() == lower_identifier:
            return member
    for member in members:
        if member.name.casefold() == lower_identifier:
            return member
    return None


def _list_person_labels(members: Sequence[Person]) -> list[str]:
    labels: list[str] = []
    for member in members:
        label = member.person_id
        if member.name and member.name.casefold() != member.person_id.casefold():
            label = f"{label} ({member.name})"
        labels.append(label)
    return sorted(labels)
