"""Run a command and its subcommands.

The execution machinery itself, free of the host: it runs inside the
command's isolated environment, which the host entry that starts the run
(``guildbotics.drivers.command_runner``) boots, never here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from guildbotics.commands.agent_turn import RunLedger, run_agent_turn
from guildbotics.commands.errors import CommandError
from guildbotics.commands.models import CommandOutcome, CommandSpec
from guildbotics.commands.spec_factory import CommandSpecFactory
from guildbotics.runtime.context import Context


class CommandRunner:
    """Coordinate the execution of main and sub commands.

    Args:
        context: The run's context.
        command_name: The main command, by the name it was asked for.
        command_args: Its arguments.
        cwd: Its working directory, which the host names.
        path: Its file, as the host resolved it: the main command is never
            resolved again by name.
        mounts: Where the environment lets a command work
            (:func:`~guildbotics.intelligences.agent_runtime.host_client.admits`):
            a subcommand working anywhere else is refused.
        ledger: The host's run record, which an invocation driven until its
            run records completion reads and reports to; without one it is
            refused.
    """

    def __init__(
        self,
        context: Context,
        command_name: str,
        command_args: Sequence[str],
        cwd: Path,
        *,
        path: Path,
        mounts: Mapping[str, bool],
        ledger: RunLedger | None = None,
    ) -> None:
        context.set_invoker(self._invoke)
        self._ledger = ledger
        self.context = context
        self.command_name = command_name
        self._command_args = list(command_args)
        self._registry: dict[str, CommandSpec] = {}
        self._call_stack: list[str] = []
        self.cwd = cwd
        self._spec_factory = CommandSpecFactory(context, mounts)
        self._main_spec = self._spec_factory.prepare_main_spec(
            path, command_name, self._command_args, cwd
        )

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
