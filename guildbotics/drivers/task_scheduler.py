import asyncio
import datetime
import threading
import time

from guildbotics.drivers.utils import run_command
from guildbotics.entities import Person, ScheduledCommand
from guildbotics.observability import trace_scope
from guildbotics.runtime import Context

DEFAULT_ROUTINE_INTERVAL_MINUTES = 10


class TaskScheduler:
    def __init__(
        self,
        context: Context,
        default_routine_commands: list[str],
        consecutive_error_limit: int = 3,
        routine_interval_minutes: int = DEFAULT_ROUTINE_INTERVAL_MINUTES,
        service_run_id: str | None = None,
    ):
        """
        Initialize the TaskScheduler with a list of jobs.
        Args:
            context (Context): The context for the task scheduler.
            default_routine_commands (list[str]): List of default routine commands to run.
            consecutive_error_limit (int): Maximum number of consecutive errors allowed
                before stopping the worker loop.
            routine_interval_minutes (int): Minimum interval between routine command
                executions for each worker.
        """
        self.context = context
        self.default_routine_commands = default_routine_commands
        # Stop the scheduling loop for a worker when this many errors occur consecutively.
        # A non-positive value is treated as 1 to avoid infinite loops on error.
        self.consecutive_error_limit = max(1, int(consecutive_error_limit))
        self.routine_interval_minutes = max(1, int(routine_interval_minutes))
        self.service_run_id = service_run_id
        self.scheduled_tasks_list = {
            p: p.get_scheduled_commands() for p in context.team.members
        }
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

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

    def get_status_summary(self) -> dict[str, int]:
        """Return lightweight runtime counters for GUI status displays."""
        active_member_count = sum(
            1 for person in self.context.team.members if person.is_active
        )
        worker_count = sum(1 for thread in self._threads if thread.is_alive())
        return {
            "active_member_count": active_member_count,
            "worker_count": worker_count,
            "routine_interval_minutes": self.routine_interval_minutes,
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

        routine_commands = (
            person.routine_commands
            if person.routine_commands
            else self.default_routine_commands
        )
        routine_command_index = 0
        next_routine_time: datetime.datetime | None = None
        consecutive_errors = 0
        try:
            while not self._stop_event.is_set():
                start_time = datetime.datetime.now()
                context.logger.debug(
                    f"Checking tasks at {start_time:%Y-%m-%d %H:%M:%S}."
                )

                # Run scheduled tasks
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
                            ok = loop.run_until_complete(
                                run_command(
                                    context, scheduled_task.command, "scheduled"
                                )
                            )
                        consecutive_errors, should_stop = (
                            self._update_consecutive_errors(
                                ok,
                                source="scheduled",
                                consecutive_errors=consecutive_errors,
                            )
                        )
                        if should_stop:
                            return
                    if self._stop_event.is_set():
                        break
                    self._sleep_interruptible(1)

                # Check for tasks to work on
                if self._stop_event.is_set():
                    break

                routine_due = (
                    next_routine_time is None or start_time >= next_routine_time
                )
                routine_command = ""
                if routine_commands and routine_due:
                    routine_command = routine_commands[
                        routine_command_index % len(routine_commands)
                    ]
                    routine_command_index += 1

                if routine_command and not self._stop_event.is_set():
                    with trace_scope(
                        "routine",
                        person_id=person.person_id,
                        command=routine_command,
                        attributes={"service_run_id": self.service_run_id},
                    ):
                        ok = loop.run_until_complete(
                            run_command(context, routine_command, "routine")
                        )
                    next_routine_time = datetime.datetime.now() + datetime.timedelta(
                        minutes=self.routine_interval_minutes
                    )
                    if not ok and not self._stop_event.is_set():
                        consecutive_errors, should_stop = (
                            self._update_consecutive_errors(
                                ok,
                                source="routine",
                                consecutive_errors=consecutive_errors,
                            )
                        )
                        if should_stop:
                            return
                    else:
                        consecutive_errors, _ = self._update_consecutive_errors(
                            ok, source="routine", consecutive_errors=consecutive_errors
                        )
                    self._sleep_interruptible(1)

                # Sleep until the next minute
                end_time = datetime.datetime.now()
                running_time = (end_time - start_time).total_seconds()
                sleep_sec = 60 - running_time
                if sleep_sec > 0 and not self._stop_event.is_set():
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

    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep in small steps so the stop event can interrupt waits."""
        # Use wait to allow immediate wake-up on shutdown.
        self._stop_event.wait(timeout=seconds)

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
