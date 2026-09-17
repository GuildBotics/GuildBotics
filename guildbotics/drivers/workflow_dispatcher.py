from __future__ import annotations

import shlex

from guildbotics.drivers.command_runner import CommandRunner
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
        events that say the execution started and ended. This dispatcher only
        adds the invocation's attributes to it.
        """
        attributes = {"service_run_id": self._service_run_id}
        if invocation.trigger_type == "chat":
            payload = invocation.payload
            event_dict = payload.get("event") or {}
            attributes.update(
                {
                    "event.provider": payload.get("service_name", ""),
                    "slack.channel": payload.get("channel_id", ""),
                    "slack.thread_ts": event_dict.get("thread_ts", ""),
                    "slack.ts": event_dict.get("message_ts", ""),
                    "event_id": event_dict.get("event_id", ""),
                }
            )

        set_attributes(**attributes)
        context = self._context.clone_for(person)
        context.shared_state[WORKFLOW_INVOCATION_KEY] = invocation

        try:
            words = shlex.split(invocation.command)
            if not words:
                raise ValueError("Empty command string in workflow invocation")
            await CommandRunner(context, words[0], words[1:]).run()
        finally:
            await context.aclose()
