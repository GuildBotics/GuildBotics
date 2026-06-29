import asyncio
import datetime
import threading
import time
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any

from guildbotics.drivers.pending_chat_dispatcher import PendingChatDispatcher
from guildbotics.drivers.utils import run_command
from guildbotics.entities import Person, ScheduledCommand
from guildbotics.observability import trace_scope
from guildbotics.runtime import Context

DEFAULT_ROUTINE_INTERVAL_MINUTES = 10
DEFAULT_CHAT_POLL_INTERVAL_SECONDS = 5.0


class TaskScheduler:
    def __init__(
        self,
        context: Context,
        default_routine_commands: list[str],
        consecutive_error_limit: int = 3,
        routine_interval_minutes: int = DEFAULT_ROUTINE_INTERVAL_MINUTES,
        service_run_id: str | None = None,
        scheduled_source_enabled: bool = True,
        routine_source_enabled: bool = True,
        event_queue_source_enabled: bool = True,
    ):
        """
        Initialize the TaskScheduler with a list of jobs.
        Args:
            context (Context): The context for the task scheduler.
            default_routine_commands (list[str]): Legacy constructor input retained for
                callers that still pass service-level routine commands. Worker execution
                uses each member's ``routine_commands``.
            consecutive_error_limit (int): Maximum number of consecutive errors allowed
                before stopping the worker loop.
            routine_interval_minutes (int): Minimum interval between routine command
                executions for each worker.
            scheduled_source_enabled (bool): Whether to run scheduled commands.
            routine_source_enabled (bool): Whether to run routine commands.
            event_queue_source_enabled (bool): Whether to drain queued chat events.
        """
        self.context = context
        self.default_routine_commands = default_routine_commands
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
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        # Queued chat events are executed here, in each member's single worker
        # thread, so a member's chat / ticket / scheduled / routine work shares
        # one serial queue and never runs two agents in the same workspace.
        self._chat_poll_interval = DEFAULT_CHAT_POLL_INTERVAL_SECONDS
        self._chat_dispatcher = PendingChatDispatcher(
            context, service_run_id=service_run_id
        )

    def start(self):
        """
        Start the task scheduler.
        """
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

    def shutdown(self, graceful: bool = True, timeout: float | None = None) -> None:
        """Signal all worker threads to stop and wait for them.

        Args:
            graceful: When True, allow current iteration to complete before exit.
            timeout: Maximum total seconds to wait for worker threads. None waits forever.
        """
        # Currently, graceful and forceful behave the same at thread level.
        # The stop event is checked between operations and during sleeps.
        self._stop_event.set()
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
        return {
            "active_member_count": active_member_count,
            "worker_count": worker_count,
            "routine_interval_minutes": self.routine_interval_minutes,
            "scheduled_source_enabled": self.scheduled_source_enabled,
            "routine_source_enabled": self.routine_source_enabled,
            "event_queue_source_enabled": self.event_queue_source_enabled,
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
                ):
                    ok = self._run(
                        loop,
                        run_command(context, scheduled_task.command, "scheduled"),
                    )
                consecutive_errors, should_stop = self._update_consecutive_errors(
                    ok,
                    source="scheduled",
                    consecutive_errors=consecutive_errors,
                )
                if should_stop:
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
        routine_due = next_routine_time is None or start_time >= next_routine_time
        routine_command = ""
        if self.routine_source_enabled and routine_commands and routine_due:
            routine_command = routine_commands[
                routine_command_index % len(routine_commands)
            ]
            routine_command_index += 1

        if routine_command and not self._stop_event.is_set():
            if routine_command == "workflows/ticket_driven_workflow":
                ok = self._run(
                    loop,
                    self._run_routine_ticket_workflow(context, person, routine_command),
                )
            else:
                with trace_scope(
                    "routine",
                    person_id=person.person_id,
                    command=routine_command,
                    attributes={"service_run_id": self.service_run_id},
                ):
                    ok = self._run(
                        loop, run_command(context, routine_command, "routine")
                    )
            next_routine_time = datetime.datetime.now() + datetime.timedelta(
                minutes=self.routine_interval_minutes
            )
            if not ok and self._stop_event.is_set():
                pass
            else:
                consecutive_errors, should_stop = self._update_consecutive_errors(
                    ok,
                    source="routine",
                    consecutive_errors=consecutive_errors,
                )
                if should_stop:
                    return (
                        routine_command_index,
                        consecutive_errors,
                        next_routine_time,
                        True,
                    )
            self._sleep_interruptible(1)

        return routine_command_index, consecutive_errors, next_routine_time, False

    async def _run_routine_ticket_workflow(
        self, context: Context, person: Person, command: str
    ) -> bool:
        """Run routine ticket workflow via selector and dispatcher."""
        from guildbotics.drivers.ticket_selector import TicketSelector
        from guildbotics.drivers.utils import run_with_logging
        from guildbotics.drivers.workflow_dispatcher import WorkflowDispatcher
        from guildbotics.observability import span_scope

        async def _action() -> None:
            with span_scope("routine_select"):
                selector = TicketSelector(context)
                invocation = await selector.select(person)

            if invocation is None:
                context.logger.info(
                    f"No active ticket task found for person '{person.person_id}'."
                )
                return

            dispatcher = WorkflowDispatcher(context, service_run_id=self.service_run_id)
            await dispatcher.dispatch(invocation, person)

        with trace_scope(
            "routine",
            person_id=person.person_id,
            command=command,
            attributes={"service_run_id": self.service_run_id},
        ):
            return await run_with_logging(context, command, "routine", _action)

    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep in small steps so the stop event can interrupt waits."""
        # Use wait to allow immediate wake-up on shutdown.
        self._stop_event.wait(timeout=seconds)

    def _run(self, loop: asyncio.AbstractEventLoop, coro: Coroutine) -> Any:
        """Run a coroutine to completion, cancelling it when a stop is requested.

        Cancellation propagates into a running agent subprocess (which the CLI
        agent brain kills), so a long agent turn cannot block shutdown past the
        stop timeout.
        """
        return loop.run_until_complete(self._run_cancellable(coro))

    async def _run_cancellable(self, coro: Coroutine) -> Any:
        task: asyncio.Task = asyncio.ensure_future(coro)
        stop_waiter = asyncio.ensure_future(self._wait_for_stop())
        try:
            await asyncio.wait({task, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await stop_waiter
        if task.done():
            return task.result()
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        return False

    async def _wait_for_stop(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(0.2)

    async def _process_pending_chat(self, person: Person) -> bool:
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
