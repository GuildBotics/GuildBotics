"""Run a command and its subcommands.

The execution machinery itself, free of the host: the isolated environment the
run's AI CLI turns share is opened by the host entry that starts the run
(``guildbotics.drivers.command_runner``), never here.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from guildbotics.commands.agent_turn import RunLedger, run_agent_turn
from guildbotics.commands.discovery import resolve_named_command
from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import command_access
from guildbotics.commands.models import CommandOutcome, CommandSpec
from guildbotics.commands.spec_factory import CommandSpecFactory
from guildbotics.runtime.context import Context


class CommandRunner:
    """Coordinate the execution of main and sub commands."""

    def __init__(
        self,
        context: Context,
        command_name: str,
        command_args: Sequence[str],
        cwd: Path | None = None,
        *,
        ledger: RunLedger | None = None,
    ) -> None:
        context.set_invoker(self._invoke)
        #: The host's run record, which an invocation driven until its run
        #: records completion reads and reports to; without one it is refused.
        self._ledger = ledger
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
            if self._ledger is None:
                raise CommandError(
                    f"'{name}' must record completion, but this run has no run ledger."
                )

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
                ledger=self._ledger,
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
