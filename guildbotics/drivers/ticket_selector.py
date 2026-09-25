"""Host-side selection and settlement of ticket work.

Every route that runs the ticket workflow -- the patrol, a scheduled command, a
manual run -- selects its ticket here and runs the workflow through
``TicketSelector.run``, the way the chat dispatcher runs its workflow through
``ChatSelector``. The selector owns what surrounds the AI CLI turn: moving the
ticket to the working lane, the run id and completion budget the turn is driven
with, and the status comment a failed or rate-limited run leaves on the ticket.
The workflow only runs the turn with the input selected here.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from typing import Any
from uuid import uuid4

from guildbotics.capabilities.workflow_rate_limits import (
    record_workflow_rate_limited,
    workflow_rate_limit_from_exception,
    workflow_rate_limit_notice_text,
)
from guildbotics.entities import Person, Task
from guildbotics.integrations.ticket_manager import TicketManager
from guildbotics.integrations.workflow_status_comment import (
    render_workflow_status_comment,
    workflow_status_comment_payload,
)
from guildbotics.observability import current_trace, set_attributes
from guildbotics.runtime.context import Context
from guildbotics.runtime.workflow_invocation import (
    TICKET_WORKFLOW_COMMAND,
    WorkflowInvocation,
    WorkflowSource,
)
from guildbotics.utils.i18n_tool import t

TICKET_MAX_ATTEMPTS_ENV = "GUILDBOTICS_TICKET_MAX_ATTEMPTS"
_DEFAULT_MAX_ATTEMPTS = 5


class TicketSelector:
    """Select a member's actionable ticket work and settle how its run ended."""

    def __init__(self, context: Context, *, source: WorkflowSource = "routine") -> None:
        self._context = context
        self._source = source

    async def candidates(self, person: Person) -> list[Task]:
        """List actionable ticket work in patrol order."""
        context = self._context.clone_for(person)
        try:
            ticket_manager = context.get_ticket_manager()
            return await ticket_manager.get_task_candidates()
        finally:
            await context.aclose()

    async def refresh(
        self, person: Person, candidate: Task
    ) -> WorkflowInvocation | None:
        """Re-read a candidate and rebuild its invocation if work remains."""
        context = self._context.clone_for(person)
        try:
            ticket_manager = context.get_ticket_manager()
            task = await ticket_manager.refresh_task(candidate)
            if task is None:
                return None
            return await self._invocation(person, ticket_manager, task)
        finally:
            await context.aclose()

    async def run_next(
        self,
        person: Person,
        run_workflow: Callable[[WorkflowInvocation], Awaitable[Any]],
    ) -> Any:
        """Run the workflow for the first ticket in patrol order, if any.

        A one-off run (a manual or scheduled command) takes the ticket the
        patrol would take next.

        Returns:
            What ``run_workflow`` returned, or ``None`` when there is no work.
        """
        candidates = await self.candidates(person)
        invocation = await self.refresh(person, candidates[0]) if candidates else None
        if invocation is None:
            return None
        return await self.run(person, invocation, run_workflow)

    async def run(
        self,
        person: Person,
        invocation: WorkflowInvocation,
        run_workflow: Callable[[WorkflowInvocation], Awaitable[Any]],
    ) -> Any:
        """Run the workflow for one selected ticket and settle how it ended.

        The run is its trace: the route that took the ticket recorded the run
        under the trace id, and the member's completion lands on that same
        record. Without a trace (a plain CLI run) the run stands alone.

        A rate limit is reported on the ticket and recorded, then settled here
        instead of re-raised as chat does: the status comment keeps the ticket
        out of selection until the reset, so the run is no worker error. Any
        other failure is reported on the ticket and re-raised.

        Returns:
            What ``run_workflow`` returned, or the rate-limit notice posted on
            the ticket.
        """
        task = Task.model_validate(invocation.payload["task"])
        set_attributes(**task.trace_attributes())
        trace = current_trace()
        run_id = trace.trace_id if trace is not None else uuid4().hex
        context = self._context.clone_for(person)
        try:
            ticket_manager = context.get_ticket_manager()
            try:
                if task.status == Task.READY and task.id is not None:
                    await ticket_manager.move_ticket(task, Task.IN_PROGRESS)
                return await run_workflow(
                    replace(
                        invocation,
                        payload={
                            **invocation.payload,
                            "run_id": run_id,
                            "max_completion_attempts": _max_agent_attempts(),
                        },
                    )
                )
            except Exception as exc:
                notice = await self._settle_failure(
                    context, ticket_manager, invocation, task, run_id, exc
                )
                if notice is None:
                    raise
                return notice
        finally:
            await context.aclose()

    async def _settle_failure(
        self,
        context: Context,
        ticket_manager: TicketManager,
        invocation: WorkflowInvocation,
        task: Task,
        run_id: str,
        exc: Exception,
    ) -> str | None:
        """Report a failed run on its ticket.

        Returns:
            The notice of a settled rate limit; ``None`` for a failure the
            caller re-raises.
        """
        rate_limit = workflow_rate_limit_from_exception(exc)
        person_id = context.person.person_id
        ticket_url = str(invocation.payload["ticket_url"])
        if rate_limit is None:
            body = await _task_error_message(context)
            payload = workflow_status_comment_payload(
                reason="failed",
                person_id=person_id,
                run_id=run_id,
                subject_id=ticket_url,
            )
        else:
            body = workflow_rate_limit_notice_text(rate_limit)
            payload = workflow_status_comment_payload(
                reason="rate_limited",
                person_id=person_id,
                run_id=run_id,
                subject_id=ticket_url,
                retry_after_at=rate_limit.retry_after_at,
                retry_after_text=rate_limit.retry_after_text,
            )
        with suppress(Exception):
            await ticket_manager.add_comment_to_ticket(
                task, render_workflow_status_comment(body=body, payload=payload)
            )
        if rate_limit is None:
            return None
        record_workflow_rate_limited(
            person_id=person_id,
            command=invocation.command,
            run_id=run_id,
            subject_id=ticket_url,
            retry_after=rate_limit,
            default_source=invocation.source,
        )
        return body

    async def _invocation(
        self, person: Person, ticket_manager: TicketManager, task: Task
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
            command=TICKET_WORKFLOW_COMMAND,
            person_id=person.person_id,
            source=self._source,
            trigger_type="ticket",
            payload=payload,
            idempotency_key=idempotency_key,
        )


def _max_agent_attempts() -> int:
    """Number of agent turns per ticket dispatch before giving up.

    A turn that leaves no terminal completion record is retried (resuming the
    previous conversation) so a slow, multi-turn AI CLI tool can finish; the
    budget bounds that so a permanently failing turn cannot loop.
    """
    raw = os.getenv(TICKET_MAX_ATTEMPTS_ENV, "").strip()
    try:
        return max(1, int(raw)) if raw else _DEFAULT_MAX_ATTEMPTS
    except ValueError:
        return _DEFAULT_MAX_ATTEMPTS


async def _task_error_message(context: Context) -> str:
    """The reader-facing failure comment, in the member's voice.

    The traceback is logged (trace-scoped ERROR) by the route that ran the
    command; the ticket comment stays a safe message with no local paths or
    internal details.
    """
    error_text = t("drivers.task_scheduler.task_error")
    try:
        from guildbotics.intelligences.functions import talk_as

        return await talk_as(context, error_text, "Ticket", []) or error_text
    except Exception:
        return error_text
