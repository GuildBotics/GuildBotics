import asyncio
import datetime
import threading
import time
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any

from guildbotics.drivers.execution import (
    ExecutionCoordinator,
    TaskRunCoordinator,
    TaskRunSyncUnavailableError,
    WorkRejectedError,
    WorkSource,
)
from guildbotics.drivers.pending_chat_dispatcher import PendingChatDispatcher
from guildbotics.drivers.utils import run_command
from guildbotics.entities import Person, ScheduledCommand, Task
from guildbotics.intelligences.agent_environment.snapshot import SnapshotUpkeep
from guildbotics.intelligences.agent_environment.status import device_status
from guildbotics.observability import trace_scope
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.runtime import Context
from guildbotics.runtime.workflow_invocation import (
    TICKET_WORKFLOW_COMMAND,
    WorkflowInvocation,
)

DEFAULT_ROUTINE_INTERVAL_MINUTES = 10
DEFAULT_CHAT_POLL_INTERVAL_SECONDS = 5.0
OWNER_RETRY_INTERVAL_SECONDS = 5.0


class TaskScheduler:
    def __init__(
        self,
        context: Context,
        consecutive_error_limit: int = 3,
        routine_interval_minutes: int = DEFAULT_ROUTINE_INTERVAL_MINUTES,
        service_run_id: str | None = None,
        scheduled_source_enabled: bool = True,
        routine_source_enabled: bool = True,
        event_queue_source_enabled: bool = True,
        execution_coordinator: ExecutionCoordinator | None = None,
    ):
        """
        Initialize the TaskScheduler with a list of jobs.
        Args:
            context (Context): The context for the task scheduler.
            consecutive_error_limit (int): Maximum number of consecutive errors allowed
                before stopping the worker loop.
            routine_interval_minutes (int): Minimum interval between routine command
                executions for each worker.
            scheduled_source_enabled (bool): Whether to run scheduled commands.
            routine_source_enabled (bool): Whether to run routine commands.
            event_queue_source_enabled (bool): Whether to drain queued chat events.
        """
        self.context = context
        # Stop the scheduling loop for a worker when this many errors occur consecutively.
        # A non-positive value is treated as 1 to avoid infinite loops on error.
        self.consecutive_error_limit = max(1, int(consecutive_error_limit))
        self.routine_interval_minutes = max(1, int(routine_interval_minutes))
        self.service_run_id = service_run_id
        self.scheduled_source_enabled = bool(scheduled_source_enabled)
        self.routine_source_enabled = bool(routine_source_enabled)
        self.event_queue_source_enabled = bool(event_queue_source_enabled)
        self.scheduled_tasks_list = {
            p: p.get_scheduled_commands() for p in context.team.members
        }
        self._execution = execution_coordinator or TaskRunCoordinator()
        # Per-member patrol heartbeat: when each member's routine slot last ran
        # and when it is next due. Read by GUI status displays, so guard the
        # dict against concurrent worker-thread updates.
        self._member_routines: dict[str, dict[str, str]] = {}
        self._member_routines_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._cancel_event = threading.Event()
        #: The last reason AI CLI work was deferred here, so it is logged once.
        self._environment_refusal = ""
        self._ticket_patrol_candidates: dict[str, list[Task]] = {}
        self._threads: list[threading.Thread] = []
        # Queued chat events are executed here, in each member's single worker
        # thread, so a member's chat / ticket / scheduled / routine work shares
        # one serial queue and never runs two agents in the same workspace.
        self._chat_poll_interval = DEFAULT_CHAT_POLL_INTERVAL_SECONDS
        self._chat_dispatcher = PendingChatDispatcher(
            context,
            service_run_id=service_run_id,
            execution_coordinator=self._execution,
        )

    def start(self):
        """
        Start the task scheduler.

        The agent environment's upkeep runs beside the member workers for as
        long as they do: a changed declaration is rebuilt here, so the next
        turn on this device boots from it without anyone asking.
        """
        SnapshotUpkeep(self._stop_event, self.context.logger).start()
        threads: list[threading.Thread] = []
        for p, scheduled_tasks in self.scheduled_tasks_list.items():
            if not p.is_active:
                continue

            thread = threading.Thread(
                target=self._process_tasks_list,
                args=(p, scheduled_tasks),
                name=p.person_id,
            )
            thread.start()
            threads.append(thread)
            self._sleep_interruptible(2)
        self._threads = threads
        # Wait on all threads (they run indefinitely)
        for thread in threads:
            thread.join()

    def request_shutdown(self, graceful: bool = True) -> None:
        """Signal worker threads to stop without waiting for them.

        Safe to call from a signal handler: it only sets events and never
        blocks, so a second signal can still be delivered to escalate a
        graceful stop into a forceful (cancelling) one.

        Args:
            graceful: When True, only stop accepting new work. When False, also
                cancel any in-flight command/workflow coroutine.
        """
        self._stop_event.set()
        if not graceful:
            self._cancel_event.set()

    def shutdown(self, graceful: bool = True, timeout: float | None = None) -> None:
        """Signal all worker threads to stop and wait for them.

        Args:
            graceful: When True, allow current iteration to complete before exit.
            timeout: Maximum total seconds to wait for worker threads. None waits forever.
        """
        # The stop event prevents new work from starting. Forceful shutdown also
        # cancels any in-flight command/workflow coroutine.
        self.request_shutdown(graceful=graceful)
        deadline = time.monotonic() + timeout if timeout is not None else None
        for t in list(self._threads):
            if t.is_alive():
                if deadline is None:
                    t.join()
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                    t.join(timeout=remaining)

    def get_status_summary(self) -> dict[str, Any]:
        """Return lightweight runtime counters for GUI status displays."""
        active_member_count = sum(
            1 for person in self.context.team.members if person.is_active
        )
        worker_count = sum(1 for thread in self._threads if thread.is_alive())
        with self._member_routines_lock:
            member_routines = sorted(
                (dict(entry) for entry in self._member_routines.values()),
                key=lambda entry: entry["person_id"],
            )
        return {
            "active_member_count": active_member_count,
            "worker_count": worker_count,
            "routine_interval_minutes": self.routine_interval_minutes,
            "scheduled_source_enabled": self.scheduled_source_enabled,
            "routine_source_enabled": self.routine_source_enabled,
            "event_queue_source_enabled": self.event_queue_source_enabled,
            "member_routines": member_routines,
        }

    def _process_tasks_list(
        self, person: Person, scheduled_tasks: list[ScheduledCommand]
    ) -> None:
        """Run the scheduling loop for a single person's tasks.

        Args:
            scheduled_tasks (list[ScheduledTask]): Tasks to check and execute.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        context = self.context.clone_for(person)

        routine_commands = person.routine_commands
        routine_command_index = 0
        next_routine_time: datetime.datetime | None = None
        consecutive_errors = 0
        try:
            while not self._stop_event.is_set():
                if isinstance(self._execution, TaskRunCoordinator):
                    authority = self._execution.service_owner_state()
                    if authority is False:
                        self.context.logger.info(
                            "Stopping scheduler because this device is no longer the service owner."
                        )
                        self._stop_event.set()
                        break
                    if authority is None:
                        self.context.logger.info(
                            "The Hub owner could not be checked; waiting before accepting service work."
                        )
                        self._sleep_interruptible(OWNER_RETRY_INTERVAL_SECONDS)
                        continue
                start_time = datetime.datetime.now()
                context.logger.debug(
                    f"Checking tasks at {start_time:%Y-%m-%d %H:%M:%S}."
                )

                if self.scheduled_source_enabled:
                    consecutive_errors, should_stop = self._process_scheduled_tasks(
                        loop,
                        context,
                        person,
                        scheduled_tasks,
                        start_time,
                        consecutive_errors,
                    )
                    if should_stop:
                        return

                # Check for tasks to work on
                if self._stop_event.is_set():
                    break

                (
                    routine_command_index,
                    consecutive_errors,
                    next_routine_time,
                    should_stop,
                ) = self._process_routine_tasks(
                    loop,
                    context,
                    person,
                    routine_commands,
                    routine_command_index,
                    next_routine_time,
                    start_time,
                    consecutive_errors,
                )
                if should_stop:
                    return

                # Wait out the rest of the minute while polling queued chat
                # events on a short cadence, so chat stays responsive without
                # speeding up the cron-granular scheduled/routine checks.
                end_time = datetime.datetime.now()
                running_time = (end_time - start_time).total_seconds()
                sleep_sec = max(0.0, 60 - running_time)
                if self.event_queue_source_enabled:
                    self._sleep_with_chat(loop, person, sleep_sec)
                elif sleep_sec > 0 and not self._stop_event.is_set():
                    next_check_time = end_time + datetime.timedelta(seconds=sleep_sec)
                    self.context.logger.debug(
                        f"Sleeping until {next_check_time:%Y-%m-%d %H:%M:%S}."
                    )
                    self._sleep_interruptible(sleep_sec)
                self.last_checked = start_time
        finally:
            try:
                loop.run_until_complete(context.aclose())
            finally:
                loop.close()

    def _process_scheduled_tasks(
        self,
        loop: asyncio.AbstractEventLoop,
        context: Context,
        person: Person,
        scheduled_tasks: list[ScheduledCommand],
        start_time: datetime.datetime,
        consecutive_errors: int,
    ) -> tuple[int, bool]:
        """Check and execute scheduled tasks."""
        for scheduled_task in scheduled_tasks:
            if self._stop_event.is_set():
                break
            if scheduled_task.should_run(start_time):
                with trace_scope(
                    "scheduled",
                    person_id=person.person_id,
                    command=scheduled_task.command,
                    attributes={"service_run_id": self.service_run_id},
                ) as trace:
                    ok = self._run_work(
                        loop,
                        person,
                        "scheduled",
                        scheduled_task.command,
                        run_command(context, scheduled_task.command, "scheduled"),
                        work_id=trace.trace_id,
                        work_identity=_slot_identity(
                            "scheduled", person, scheduled_task.command, start_time
                        ),
                    )
                consecutive_errors, should_stop = self._update_consecutive_errors(
                    ok,
                    source="scheduled",
                    consecutive_errors=consecutive_errors,
                )
                if should_stop:
                    self._record_worker_failed(
                        person,
                        source="scheduled",
                        consecutive_errors=consecutive_errors,
                    )
                    return consecutive_errors, True
            if self._stop_event.is_set():
                break
            self._sleep_interruptible(1)
        return consecutive_errors, False

    def _process_routine_tasks(
        self,
        loop: asyncio.AbstractEventLoop,
        context: Context,
        person: Person,
        routine_commands: list[str],
        routine_command_index: int,
        next_routine_time: datetime.datetime | None,
        start_time: datetime.datetime,
        consecutive_errors: int,
    ) -> tuple[int, int, datetime.datetime | None, bool]:
        """Check and execute routine tasks, routing ticket workflows through the selector."""
        pending_ticket_patrol = bool(
            self._ticket_patrol_candidates.get(person.person_id)
        )
        routine_due = next_routine_time is None or start_time >= next_routine_time
        routine_command = ""
        if self.routine_source_enabled and pending_ticket_patrol:
            routine_command = TICKET_WORKFLOW_COMMAND
        elif self.routine_source_enabled and routine_commands and routine_due:
            routine_command = routine_commands[
                routine_command_index % len(routine_commands)
            ]
            routine_command_index += 1

        if routine_command and not self._stop_event.is_set():
            # Any other routine, one naming the ticket workflow with arguments
            # included, runs as a command whose entry selects its ticket.
            ticket_patrol = routine_command == TICKET_WORKFLOW_COMMAND
            if ticket_patrol and self._environment_unavailable():
                # The AI CLI turn the patrol would dispatch cannot start here
                # yet (the environment is being built, or is not set up). It
                # is deferred, not failed: the worker stays up and the ticket
                # is picked again once the device is ready.
                self._ticket_patrol_candidates.pop(person.person_id, None)
                ok = True
                patrol_exhausted = True
            elif ticket_patrol:
                ok, patrol_exhausted = self._patrol_tickets(
                    loop, context, person, routine_command, start_time
                )
            else:
                patrol_exhausted = True
                with trace_scope(
                    "routine",
                    person_id=person.person_id,
                    command=routine_command,
                    attributes={"service_run_id": self.service_run_id},
                ) as trace:
                    ok = self._run_work(
                        loop,
                        person,
                        "routine",
                        routine_command,
                        run_command(context, routine_command, "routine"),
                        work_id=trace.trace_id,
                        work_identity=_slot_identity(
                            "routine", person, routine_command, start_time
                        ),
                    )
            if patrol_exhausted:
                now = datetime.datetime.now()
                next_routine_time = now + datetime.timedelta(
                    minutes=self.routine_interval_minutes
                )
                self._record_member_routine(person, last=now, next_at=next_routine_time)
            else:
                next_routine_time = None
            consecutive_errors, should_stop = self._update_consecutive_errors(
                ok,
                source="routine",
                consecutive_errors=consecutive_errors,
            )
            if should_stop:
                self._record_worker_failed(
                    person, source="routine", consecutive_errors=consecutive_errors
                )
                return (
                    routine_command_index,
                    consecutive_errors,
                    next_routine_time,
                    True,
                )
            self._sleep_interruptible(1)

        return routine_command_index, consecutive_errors, next_routine_time, False

    def _record_worker_failed(
        self, person: Person, *, source: str, consecutive_errors: int
    ) -> None:
        record_correlated_event(
            event_type="scheduler.worker.failed",
            default_source="scheduler",
            person_id=person.person_id,
            attributes={"service_run_id": self.service_run_id or ""},
            payload={
                "source": source,
                "consecutive_errors": consecutive_errors,
                "consecutive_error_limit": self.consecutive_error_limit,
            },
        )

    def _record_member_routine(
        self, person: Person, *, last: datetime.datetime, next_at: datetime.datetime
    ) -> None:
        with self._member_routines_lock:
            self._member_routines[person.person_id] = {
                "person_id": person.person_id,
                "last_routine_at": last.astimezone().isoformat(),
                "next_routine_at": next_at.astimezone().isoformat(),
            }

    def _patrol_tickets(
        self,
        loop: asyncio.AbstractEventLoop,
        context: Context,
        person: Person,
        command: str,
        start_time: datetime.datetime,
    ) -> tuple[bool, bool]:
        """Refresh and dispatch one candidate from the current patrol batch.

        Selection runs outside any trace and outside the execution boundary:
        an idle patrol (no actionable ticket) leaves neither diagnostics
        records nor a task-run record. Both are opened only for a ticket that
        is actually dispatched, so a run record means work was taken; a
        selection that failed opens only a trace, which records it as the
        failure it is. The trace opens with the ticket's attributes so the run
        names its PR / issue from its first record, and the selector settles
        the ticket around the workflow it runs.
        """
        from guildbotics.drivers.ticket_selector import TicketSelector
        from guildbotics.drivers.utils import run_with_logging
        from guildbotics.drivers.workflow_dispatcher import WorkflowDispatcher

        attributes: dict[str, Any] = {"service_run_id": self.service_run_id}
        selector = TicketSelector(context)
        candidates = self._ticket_patrol_candidates.setdefault(person.person_id, [])
        try:
            if not candidates:
                candidates.extend(self._run(loop, selector.candidates(person)))
                if not candidates:
                    self._ticket_patrol_candidates.pop(person.person_id, None)
                    context.logger.debug(
                        f"No active ticket task found for person '{person.person_id}'."
                    )
                    return True, True
            candidate = candidates.pop(0)
            invocation = self._run(loop, selector.refresh(person, candidate))
        except Exception as exc:
            # Bind outside the except block: Python unbinds `exc` when the
            # block exits, but the closure runs inside run_with_logging.
            failure = exc

            async def _reraise() -> None:
                raise failure

            with (
                trace_scope(
                    "routine",
                    person_id=person.person_id,
                    command=command,
                    attributes=attributes,
                ),
                suppress(Exception),
            ):
                self._run(loop, run_with_logging(context, command, "routine", _reraise))
            return False, not candidates

        if not invocation:
            if not candidates:
                self._ticket_patrol_candidates.pop(person.person_id, None)
            return True, not candidates

        async def _dispatch() -> None:
            dispatcher = WorkflowDispatcher(context, service_run_id=self.service_run_id)
            await selector.run(
                person, invocation, lambda turn: dispatcher.dispatch(turn, person)
            )

        task_payload = invocation.payload.get("task")
        if isinstance(task_payload, dict):
            attributes.update(Task.model_validate(task_payload).trace_attributes())
        with trace_scope(
            "routine",
            person_id=person.person_id,
            command=command,
            attributes=attributes,
        ) as trace:
            ok = bool(
                self._run_work(
                    loop,
                    person,
                    "routine",
                    command,
                    run_with_logging(context, command, "routine", _dispatch),
                    work_id=trace.trace_id,
                    work_identity=_ticket_slot_identity(
                        person, command, start_time, invocation
                    ),
                )
            )
        if not candidates:
            self._ticket_patrol_candidates.pop(person.person_id, None)
        return ok, not candidates

    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep in small steps so the stop event can interrupt waits."""
        # Use wait to allow immediate wake-up on shutdown.
        self._stop_event.wait(timeout=seconds)

    def _run(self, loop: asyncio.AbstractEventLoop, coro: Coroutine) -> Any:
        """Run a coroutine to completion, cancelling it when forced.

        Graceful shutdown lets the current command/workflow finish and only
        prevents the next one from starting. Forceful shutdown cancels the task,
        which propagates into a running agent subprocess.
        """
        return loop.run_until_complete(self._run_cancellable(coro))

    def _run_work(
        self,
        loop: asyncio.AbstractEventLoop,
        person: Person,
        source: WorkSource,
        command: str,
        coro: Coroutine,
        *,
        work_id: str | None = None,
        work_identity: dict[str, str] | None = None,
    ) -> Any:
        """Run one unit of work inside the task-run boundary.

        The boundary sits inside the awaited coroutine, as it does for chat, so
        both a failure and a forced cancellation reach it and close the run as
        failed or cancelled; only then is the failure turned into ``False``.
        """

        async def _tracked() -> Any:
            with self._execution.track_work(
                source=source,
                person_id=person.person_id,
                command=command,
                work_id=work_id,
                cancel=self._cancel_event.set,
                work_identity=work_identity,
            ):
                return await coro

        try:
            return self._run(loop, _tracked())
        except TaskRunSyncUnavailableError as exc:
            self.context.logger.warning(
                "Service work result is not shared yet: %s", exc
            )
            return True
        except WorkRejectedError as exc:
            if exc.reason == "draining":
                # A stop of this scheduler is already in progress; mirror it
                # locally so worker loops exit without counting a command error.
                self._stop_event.set()
                return False
            if exc.reason in {"owner_unreachable", "sync_unavailable"}:
                # A Hub outage or an unconfirmed start barrier blocks only this
                # new work item. Keep the service alive so a later polling
                # cycle can retry; an already running workflow is never
                # cancelled for a connectivity failure.
                return True
            if exc.reason == "not_owner":
                # A confirmed transfer is different from an outage: this
                # device must stop accepting service work immediately.
                self._stop_event.set()
                return True
            if exc.reason == "duplicate":
                # The same stable input already has a running or terminal run
                # on another device. It was deliberately not executed twice.
                return True
            # Another frontend is temporarily using this person. Skipping is a
            # successful scheduler cycle: the next tick can try again and other
            # members must continue running.
            self.context.logger.info(
                "Skipping %s work for %s because the person execution lease is held.",
                source,
                person.person_id,
            )
            return True
        except Exception:
            # The command already logged its failure and the boundary closed
            # the run as failed; the worker counts it as a command error.
            return False
        finally:
            # Work that was rejected or cancelled before it started was never
            # awaited; closing an awaited coroutine is a no-op.
            coro.close()

    async def _run_cancellable(self, coro: Coroutine) -> Any:
        task: asyncio.Task = asyncio.ensure_future(coro)
        cancel_waiter = asyncio.ensure_future(self._wait_for_cancel())
        try:
            await asyncio.wait(
                {task, cancel_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            cancel_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_waiter
        if task.done():
            return task.result()
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        return False

    async def _wait_for_cancel(self) -> None:
        while not self._cancel_event.is_set():
            await asyncio.sleep(0.2)

    def _environment_unavailable(self) -> bool:
        """Whether AI CLI work must wait for this device's environment.

        The reason is the one the status shows; it is logged once per change
        rather than once per poll.
        """
        reason = device_status().refusal
        if reason != self._environment_refusal:
            self._environment_refusal = reason
            if reason:
                self.context.logger.warning(
                    f"AI CLI work is deferred on this device: {reason}"
                )
            else:
                self.context.logger.info("The agent environment is ready; resuming.")
        return bool(reason)

    async def _process_pending_chat(self, person: Person) -> bool:
        if self._environment_unavailable():
            return True
        try:
            await self._chat_dispatcher.process_person(person, self._stop_event)
            return True
        except Exception as exc:
            self.context.logger.error(
                f"Error processing chat events for '{person.person_id}': {exc}"
            )
            return False

    def _sleep_with_chat(
        self, loop: asyncio.AbstractEventLoop, person: Person, total_seconds: float
    ) -> None:
        """Process queued chat events, then wait, repeating until the minute ends."""
        deadline = datetime.datetime.now() + datetime.timedelta(
            seconds=max(0.0, total_seconds)
        )
        while not self._stop_event.is_set():
            self._run(loop, self._process_pending_chat(person))
            remaining = (deadline - datetime.datetime.now()).total_seconds()
            if remaining <= 0:
                break
            self._sleep_interruptible(min(self._chat_poll_interval, remaining))

    def _update_consecutive_errors(
        self, ok: bool, *, source: str, consecutive_errors: int
    ):
        """Update error counter and decide whether to stop the worker loop.

        Args:
            ok: Result of a command execution.
            source: A short label for logging (e.g., "scheduled", "routine").
            consecutive_errors: Current consecutive error count.

        Returns:
            A tuple of (new_consecutive_errors, should_stop).
        """
        if not ok:
            if self._stop_event.is_set():
                # Shutdown is in progress: a command that was rejected or
                # cancelled by the stop is not a workflow error.
                return consecutive_errors, False
            consecutive_errors += 1
            self.context.logger.warning(
                f"Command error occurred ({source}). "
                f"consecutive_errors={consecutive_errors}/{self.consecutive_error_limit}"
            )
            if consecutive_errors >= self.consecutive_error_limit:
                self.context.logger.error(
                    "Maximum consecutive errors reached. Stopping this worker loop."
                )
                return consecutive_errors, True
            return consecutive_errors, False
        # Reset on success
        return 0, False


def _slot_identity(
    kind: str, person: Person, command: str, start_time: datetime.datetime
) -> dict[str, str]:
    """The stable identity of one scheduler slot: the input the boundary claims."""
    return {
        "kind": kind,
        "person_id": person.person_id,
        "command": command,
        "slot": start_time.replace(second=0, microsecond=0).isoformat(),
    }


def _ticket_slot_identity(
    person: Person,
    command: str,
    start_time: datetime.datetime,
    invocation: WorkflowInvocation,
) -> dict[str, str]:
    """Identify one patrol item within a scheduler slot."""
    identity = _slot_identity("routine", person, command, start_time)
    identity.update(
        {
            "ticket_url": str(invocation.payload.get("ticket_url") or ""),
            "trigger_reason": str(invocation.payload.get("trigger_reason") or ""),
        }
    )
    return identity
