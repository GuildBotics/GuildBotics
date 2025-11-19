import asyncio
import contextlib
import datetime
import threading

from guildbotics.drivers.utils import run_command
from guildbotics.entities import Person, ScheduledCommand
from guildbotics.runtime import Context


class TaskScheduler:
    def __init__(
        self,
        context: Context,
        default_routine_commands: list[str],
        consecutive_error_limit: int = 3,
    ):
        """
        Initialize the TaskScheduler with a list of jobs.
        Args:
            context (Context): The context for the task scheduler.
            default_routine_commands (list[str]): List of default routine commands to run.
            consecutive_error_limit (int): Maximum number of consecutive errors allowed
                before stopping the worker loop.
        """
        self.context = context
        self.default_routine_commands = default_routine_commands
        # Stop the scheduling loop for a worker when this many errors occur consecutively.
        # A non-positive value is treated as 1 to avoid infinite loops on error.
        self.consecutive_error_limit = max(1, int(consecutive_error_limit))
        self.scheduled_tasks_list = {
            p: p.get_scheduled_commands() for p in context.team.members
        }
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._service_threads: list[threading.Thread] = []
        # Allow long-running commands to observe shutdown intent.
        self.context.shutdown_event = self._stop_event

    def start(self):
        """
        Start the task scheduler.
        """
        threads: list[threading.Thread] = []
        service_threads: list[threading.Thread] = []
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
            service_threads.extend(self._start_service_workers(p))
        self._threads = threads
        self._service_threads = service_threads
        # Wait on all threads (they run indefinitely)
        for thread in threads + service_threads:
            thread.join()

    def shutdown(self, graceful: bool = True) -> None:
        """Signal all worker threads to stop and wait for them.

        Args:
            graceful: When True, allow current iteration to complete before exit.
        """
        # Currently, graceful and forceful behave the same at thread level.
        # The stop event is checked between operations and during sleeps.
        self._stop_event.set()
        for t in list(self._threads):
            if t.is_alive():
                t.join()
        for t in list(self._service_threads):
            if t.is_alive():
                t.join()
        self._service_threads.clear()

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
        consecutive_errors = 0
        while not self._stop_event.is_set():
            start_time = datetime.datetime.now()
            context.logger.debug(f"Checking tasks at {start_time:%Y-%m-%d %H:%M:%S}.")

            # Run scheduled tasks
            for scheduled_task in scheduled_tasks:
                if self._stop_event.is_set():
                    break
                if scheduled_task.should_run(start_time):
                    ok = loop.run_until_complete(
                        run_command(context, scheduled_task.command, "scheduled")
                    )
                    consecutive_errors, should_stop = self._update_consecutive_errors(
                        ok, source="scheduled", consecutive_errors=consecutive_errors
                    )
                    if should_stop:
                        return
                if self._stop_event.is_set():
                    break
                self._sleep_interruptible(1)

            # Check for tasks to work on
            if self._stop_event.is_set():
                break

            routine_command = (
                routine_commands[routine_command_index % len(routine_commands)]
                if routine_commands
                else ""
            )
            routine_command_index += 1

            if routine_command and not self._stop_event.is_set():
                ok = loop.run_until_complete(
                    run_command(context, routine_command, "routine")
                )
                if not ok and not self._stop_event.is_set():
                    consecutive_errors, should_stop = self._update_consecutive_errors(
                        ok, source="routine", consecutive_errors=consecutive_errors
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

    def _start_service_workers(self, person: Person) -> list[threading.Thread]:
        """Launch service command workers for the given person."""
        threads: list[threading.Thread] = []
        for index, command in enumerate(person.service_commands):
            thread = threading.Thread(
                target=self._run_service_worker,
                args=(person, command),
                name=f"{person.person_id}:service:{index}",
            )
            thread.start()
            threads.append(thread)
            self._sleep_interruptible(0.1)
        return threads

    def _run_service_worker(self, person: Person, command: str) -> None:
        """Run a long-lived service command with restart/backoff."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._service_loop(person, command))
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _service_loop(self, person: Person, command: str) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            context = self.context.clone_for(person)
            context.shutdown_event = self._stop_event
            task = asyncio.create_task(run_command(context, command, "service"))
            ok = await self._wait_for_service_task(task, context, command)
            if self._stop_event.is_set():
                break
            if ok:
                backoff = 1.0
            else:
                backoff = min(backoff * 2, 60.0)
            await self._async_sleep_interruptible(backoff)

    async def _wait_for_service_task(
        self, task: asyncio.Task, context: Context, command: str
    ) -> bool:
        """Wait for a service task or cancel it on shutdown."""
        while not task.done() and not self._stop_event.is_set():
            await asyncio.sleep(1)

        if self._stop_event.is_set() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return False

        if task.cancelled():
            return False
        if task.exception():
            context.logger.error(
                f"Service command '{context.person.person_id}:{command}' failed: {task.exception()}"
            )
            return False
        result = task.result()
        return bool(result)

    async def _async_sleep_interruptible(self, seconds: float) -> None:
        """Async sleep that wakes up early on shutdown."""
        remaining = seconds
        while remaining > 0 and not self._stop_event.is_set():
            step = min(1.0, remaining)
            await asyncio.sleep(step)
            remaining -= step

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
