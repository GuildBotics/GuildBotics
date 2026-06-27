from __future__ import annotations

import shlex

from guildbotics.drivers.command_runner import CommandRunner
from guildbotics.entities.team import Person
from guildbotics.observability import current_trace, set_attributes, trace_scope
from guildbotics.runtime.context import Context
from guildbotics.runtime.event_listener import INCOMING_CHAT_EVENT_KEY
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
        """Run the workflow corresponding to the invocation for the given person."""
        # 1. Map invocation source to trace scope name
        scope_name = (
            "event_listener"
            if invocation.source == "event_queue"
            else invocation.source
        )

        # 2. Gather trace attributes
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

        # 3. Execute under trace_scope if trace does not exist, else reuse active trace
        async def _execute() -> None:
            context = self._context.clone_for(person)
            context.shared_state[WORKFLOW_INVOCATION_KEY] = invocation

            # Maintain backward compatibility for chat workflow
            if invocation.trigger_type == "chat":
                context.shared_state[INCOMING_CHAT_EVENT_KEY] = invocation.payload

            try:
                words = shlex.split(invocation.command)
                if not words:
                    raise ValueError("Empty command string in workflow invocation")
                await CommandRunner(context, words[0], words[1:]).run()
            finally:
                await context.aclose()

        if current_trace() is None:
            with trace_scope(
                scope_name,
                person_id=person.person_id,
                command=invocation.command,
                attributes=attributes,
            ):
                await _execute()
        else:
            set_attributes(**attributes)
            await _execute()
