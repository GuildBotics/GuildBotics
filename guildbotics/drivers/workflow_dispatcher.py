from __future__ import annotations

from guildbotics.drivers.command_runner import prepare_host_command, run_in_environment
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
            await run_in_environment(prepare_host_command(context, invocation.command))
        finally:
            await context.aclose()
