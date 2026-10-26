"""Host-side selection and settlement of ticket work.

Every route that runs the ticket workflow -- the patrol, a scheduled command, a
manual run -- selects its ticket here and runs the workflow through
``TicketSelector.run``, the way the chat dispatcher runs its workflow through
``ChatSelector``. The selector owns what surrounds the AI CLI turn: moving the
ticket to the working lane, the run id and completion budget the turn is driven
with, and the status comment a failed or rate-limited run leaves on the ticket.
The workflow only runs the turn with the input selected here. When the work
the run leaves waits for a person, the selector has the member announce it in
chat.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from typing import Any

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.capabilities.work_stops import (
    WorkStop,
    completed_stop,
    notify_work_stop,
)
from guildbotics.capabilities.workflow_rate_limits import (
    record_workflow_rate_limited,
    workflow_rate_limit_from_exception,
    workflow_rate_limit_notice_text,
)
from guildbotics.drivers.execution import failure_status
from guildbotics.entities import Person, Task
from guildbotics.entities.team import Service
from guildbotics.integrations import pull_request_patrol
from guildbotics.intelligences.agent_runtime.models import TURN_TIMEOUT_SECONDS
from guildbotics.intelligences.common import find_cli_agent_execution_error
from guildbotics.runtime.context import Context
from guildbotics.runtime.member_invocation import Work
from guildbotics.runtime.port import WorkflowStatus
from guildbotics.runtime.workflow_invocation import (
    TICKET_WORKFLOW_COMMAND,
    WorkflowInvocation,
    WorkflowSource,
)
from guildbotics.utils.correlation import require_trace, set_attributes
from guildbotics.utils.i18n_tool import t

TICKET_MAX_ATTEMPTS_ENV = "GUILDBOTICS_TICKET_MAX_ATTEMPTS"
_DEFAULT_MAX_ATTEMPTS = 5


class TicketSelector:
    """Select a member's actionable ticket work and settle how its run ended."""

    def __init__(self, context: Context, *, source: WorkflowSource = "routine") -> None:
        self._context = context
        self._source = source

    async def candidates(self, person: Person) -> list[Task]:
        """List actionable ticket work in patrol order: the pull requests
        first, then the board's tickets."""
        context = self._context.clone_for(person)
        try:
            pull_requests = (
                await context.get_code_hosting_service().pull_request_candidates()
                if _has_code_host(context)
                else []
            )
            return [*pull_requests, *await context.get_ticket_service().candidates()]
        finally:
            await context.aclose()

    async def refresh(
        self, person: Person, candidate: Task
    ) -> WorkflowInvocation | None:
        """Re-read a candidate and rebuild its invocation if work remains."""
        context = self._context.clone_for(person)
        try:
            task = await (
                context.get_code_hosting_service().refresh_pull_request(candidate)
                if candidate.pull_request_url
                else context.get_ticket_service().refresh(candidate)
            )
            if task is None:
                return None
            return self._invocation(person, task)
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
        record. A run the patrol resumed for its PR's checks ends their wait
        unless it awaits them again (:func:`_settle_checks`); it runs in the
        work the awaiting run did, and answers for the PR whatever that work
        is. A run of autonomous
        work that ends asking or blocked, or fails and says so on its item,
        waits for a person: the member announces it in chat, once, since the
        member's comment keeps the work out of selection until someone
        answers. That holds while the comment is all the item waits on: on the
        member's own PR, any thread whose last word is someone else's keeps it
        selected (:func:`~guildbotics.integrations.pull_request_patrol.pull_request_work`),
        so a run that asks in a conversation comment and leaves such a thread
        unanswered is taken up, and announced, again on every patrol.

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
        run_id = require_trace().trace_id
        context = self._context.clone_for(person)
        try:
            try:
                if (
                    not (invocation.work and invocation.work.kind == "ticket_mention")
                    and task.status == Task.READY
                    and task.id is not None
                ):
                    await context.get_ticket_service().move(task, "in_progress")
                if task.resumes is not None:
                    # What the run answers for, whatever work it resumes.
                    RunStore().append_evidence(
                        run_id,
                        RunStore.CHECKS_RESUMED_EVIDENCE,
                        {
                            "target": {
                                "kind": "pull_request",
                                "url": task.url,
                            }
                        },
                    )
                result = await run_workflow(
                    replace(
                        invocation,
                        run_id=run_id,
                        payload={
                            **invocation.payload,
                            "max_completion_attempts": _max_agent_attempts(),
                        },
                    )
                )
            except Exception as exc:
                if failure_status(exc, self._source) == "deferred":
                    raise
                notice = await self._settle_failure(
                    context, task, invocation, run_id, exc
                )
                if notice is None:
                    raise
                return notice
            await _settle_checks(context, task, run_id)
            if self._source != "manual" and (stop := completed_stop(task, run_id)):
                await notify_work_stop(context, stop)
            return result
        finally:
            await context.aclose()

    async def _settle_failure(
        self,
        context: Context,
        task: Task,
        invocation: WorkflowInvocation,
        run_id: str,
        exc: Exception,
    ) -> str | None:
        """Report a failed run on its ticket, and announce in chat that the
        work waits for a person once that report landed.

        Returns:
            The notice of a settled rate limit; ``None`` for a failure the
            caller re-raises.
        """
        rate_limit = workflow_rate_limit_from_exception(exc)
        person_id = context.person.person_id
        ticket_url = str(invocation.payload["ticket_url"])
        if rate_limit is None:
            error_text = (
                t(
                    "drivers.task_scheduler.turn_timeout",
                    hours=TURN_TIMEOUT_SECONDS // 3600,
                )
                if find_cli_agent_execution_error(exc, category="timeout") is not None
                else t("drivers.task_scheduler.task_error")
            )
            body = await _task_error_message(context, error_text)
            status = WorkflowStatus(
                reason="failed", person_id=person_id, run_id=run_id, subject=ticket_url
            )
        else:
            body = workflow_rate_limit_notice_text(rate_limit, workflow="ticket")
            status = WorkflowStatus(
                reason="rate_limited",
                person_id=person_id,
                run_id=run_id,
                subject=ticket_url,
                retry_after_at=rate_limit.retry_after_at,
                retry_after_text=rate_limit.retry_after_text,
            )
        # The status is said where the work came from: a pull request on the
        # code host, a ticket on the ticket service.
        written = None
        with suppress(Exception):
            written = await (
                context.get_code_hosting_service()
                if invocation.payload["pull_request_url"]
                else context.get_ticket_service()
            ).comment(ticket_url, body, status=status)
        if rate_limit is None:
            if written is not None and self._source != "manual":
                await notify_work_stop(
                    context, WorkStop.of(task, "failed", run_id, written.url or "")
                )
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

    def _invocation(self, person: Person, task: Task) -> WorkflowInvocation:
        """The run of ``task``, in the work it is: a ticket's, or -- for a PR
        whose checks the member awaits -- the work the awaiting run did, whose
        conversation it continues, with that run's summary of where the work
        came from as far as this device has it: a run whose record has not
        arrived, or which never recorded a summary, gives none, and the work
        is resumed all the same rather than held by a record that may never
        come."""
        assert task.url
        ticket_url = task.url
        resumes = task.resumes
        if resumes is not None:
            work = Work(resumes.work_kind, resumes.work_identity)
        elif (
            task.assignee != person.person_id and task.trigger_reason == "issue_mention"
        ):
            work = Work("ticket_mention", ticket_url)
        else:
            work = Work.of_ticket(ticket_url)
        payload = {
            "task": task.model_dump(),
            "ticket_url": ticket_url,
            "pull_request_url": task.pull_request_url or "",
            "trigger_reason": task.trigger_reason or "",
            "awaited_run_id": resumes.run_id if resumes is not None else "",
            "awaited_summary": (
                RunStore().summary(resumes.run_id) if resumes is not None else ""
            ),
        }
        return WorkflowInvocation(
            command=TICKET_WORKFLOW_COMMAND,
            person_id=person.person_id,
            source=self._source,
            trigger_type="ticket",
            payload=payload,
            work=work,
        )


async def _settle_checks(context: Context, task: Task, run_id: str) -> None:
    """End the wait on the PR a run was resumed for, unless the run awaits
    that PR's checks again: the wait is the member's latest status notice on
    the PR, and whatever the run itself wrote there, or awaited elsewhere, it
    would resume the member on every patrol."""
    if task.resumes is None or not task.pull_request_url:
        return
    # Like a failure's notice, this one cannot fail the run that ended: the
    # wait it leaves resumes the member on the next patrol instead.
    with suppress(Exception):
        if task.pull_request_url in RunStore().awaited_pull_requests(run_id):
            return
        await pull_request_patrol.checks_settled(
            context.get_code_hosting_service(),
            task.pull_request_url,
            person_id=context.person.person_id,
            run_id=run_id,
        )


def _has_code_host(context: Context) -> bool:
    return context.team.project.is_available_service(Service.CODE_HOSTING_SERVICE)


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


async def _task_error_message(context: Context, error_text: str) -> str:
    """The reader-facing failure comment, in the member's voice.

    The traceback is logged (trace-scoped ERROR) by the route that ran the
    command; the ticket comment stays a safe message with no local paths or
    internal details.
    """
    try:
        from guildbotics.intelligences.functions import talk_as

        return await talk_as(context, error_text, "Ticket", []) or error_text
    except Exception:
        return error_text
