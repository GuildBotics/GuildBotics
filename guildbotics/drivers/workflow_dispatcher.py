from __future__ import annotations

import shlex

from guildbotics.commands.runner import CommandRunner
from guildbotics.drivers.command_runner import run_in_environment
from guildbotics.entities.team import Person
from guildbotics.observability import set_attributes
from guildbotics.runtime.context import Context
from guildbotics.runtime.workflow_invocation import (
    WORKFLOW_INVOCATION_KEY,
    WorkflowInvocation,
)


class WorkflowDispatcher:
    """Dispatches workflows using uniform WorkflowInvocations."""

    def __init__(self, context: Context, service_run_id: str | None = None) -> None:
        self._context = context
        self._service_run_id = service_run_id

    async def dispatch(self, invocation: WorkflowInvocation, person: Person) -> None:
        """Run the workflow corresponding to the invocation for the given person.

        The caller owns the trace: it opened the scope and records the boundary
        events that say the execution started and ended, with the attributes
        that name its subject. This dispatcher only adds the service run id.
        """
        set_attributes(service_run_id=self._service_run_id)
        context = self._context.clone_for(person)
        context.shared_state[WORKFLOW_INVOCATION_KEY] = invocation

        try:
            words = shlex.split(invocation.command)
            if not words:
                raise ValueError("Empty command string in workflow invocation")
            await run_in_environment(CommandRunner(context, words[0], words[1:]))
        finally:
            await context.aclose()
