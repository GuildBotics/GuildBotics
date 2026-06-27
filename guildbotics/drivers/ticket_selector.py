from __future__ import annotations

from guildbotics.entities.team import Person
from guildbotics.runtime.context import Context
from guildbotics.runtime.workflow_invocation import WorkflowInvocation


class TicketSelector:
    """Selects one actionable GitHub ticket/PR and wraps it in a WorkflowInvocation."""

    def __init__(self, context: Context) -> None:
        self._context = context

    async def select(self, person: Person) -> WorkflowInvocation | None:
        """Select an actionable ticket for the person and build a WorkflowInvocation."""
        context = self._context.clone_for(person)
        try:
            ticket_manager = context.get_ticket_manager()
            task = await ticket_manager.get_task_to_work_on()
            if task is None:
                return None

            ticket_url = await ticket_manager.get_ticket_url(task, markdown=False)
            payload = {
                "task": task.model_dump(),
                "ticket_url": ticket_url,
                "pull_request_url": task.pull_request_url or "",
                "trigger_reason": task.trigger_reason or "",
            }
            idempotency_key = f"github:ticket:{person.person_id}:{ticket_url}:{task.pull_request_url or ''}:{task.trigger_reason or ''}"

            return WorkflowInvocation(
                command="workflows/ticket_driven_workflow",
                person_id=person.person_id,
                source="routine",
                trigger_type="ticket",
                payload=payload,
                idempotency_key=idempotency_key,
            )
        finally:
            await context.aclose()
