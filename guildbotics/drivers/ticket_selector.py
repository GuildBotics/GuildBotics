from __future__ import annotations

from guildbotics.entities import Person, Task
from guildbotics.integrations.ticket_manager import TicketManager
from guildbotics.runtime.context import Context
from guildbotics.runtime.workflow_invocation import WorkflowInvocation


class TicketSelector:
    """Build workflow invocations from actionable GitHub ticket work."""

    def __init__(self, context: Context) -> None:
        self._context = context

    async def candidates(self, person: Person) -> list[WorkflowInvocation]:
        """List actionable ticket work in patrol order."""
        context = self._context.clone_for(person)
        try:
            ticket_manager = context.get_ticket_manager()
            tasks = await ticket_manager.get_task_candidates()
            return [
                await self._invocation(person, ticket_manager, task) for task in tasks
            ]
        finally:
            await context.aclose()

    async def refresh(
        self, person: Person, candidate: WorkflowInvocation
    ) -> WorkflowInvocation | None:
        """Re-read a candidate and rebuild its invocation if work remains."""
        context = self._context.clone_for(person)
        try:
            ticket_manager = context.get_ticket_manager()
            task = await ticket_manager.refresh_task(
                self._task_from_invocation(candidate)
            )
            if task is None:
                return None
            return await self._invocation(person, ticket_manager, task)
        finally:
            await context.aclose()

    @staticmethod
    def _task_from_invocation(invocation: WorkflowInvocation) -> Task:
        return Task.model_validate(invocation.payload["task"])

    @staticmethod
    async def _invocation(
        person: Person, ticket_manager: TicketManager, task: Task
    ) -> WorkflowInvocation:
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
