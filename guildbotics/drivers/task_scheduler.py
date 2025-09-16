import asyncio
import datetime
import threading
from time import sleep

from guildbotics.drivers.utils import run_workflow
from guildbotics.entities import Person, ScheduledTask
from guildbotics.runtime import Context


class TaskScheduler:
    def __init__(self, context: Context):
        """
        Initialize the TaskScheduler with a list of jobs.
        Args:
            context (WorkflowContext): The workflow context.
        """
        self.context = context
        self.scheduled_tasks_list = {
            p: p.get_scheduled_tasks() for p in context.team.members
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
        self._threads = threads
        # Wait on all threads (they run indefinitely)
        for thread in threads:
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

    def _process_tasks_list(
        self, person: Person, scheduled_tasks: list[ScheduledTask]
    ) -> None:
        """Run the scheduling loop for a single person's tasks.

        Args:
            scheduled_tasks (list[ScheduledTask]): Tasks to check and execute.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        context = self.context.clone_for(person)
        ticket_manager = context.get_ticket_manager()

        while not self._stop_event.is_set():
            start_time = datetime.datetime.now()
            self.context.logger.debug(
                f"Checking tasks at {start_time:%Y-%m-%d %H:%M:%S}."
            )

            # Run scheduled tasks
            for scheduled_task in scheduled_tasks:
                if self._stop_event.is_set():
                    break
                if scheduled_task.should_run(start_time):
                    loop.run_until_complete(
                        run_workflow(context, scheduled_task.task, "scheduled")
                    )
                if self._stop_event.is_set():
                    break
                self._sleep_interruptible(1)

            # Check for tasks to work on
            if self._stop_event.is_set():
                break
            task = loop.run_until_complete(ticket_manager.get_task_to_work_on())
            if task and not self._stop_event.is_set():
                ok, message = loop.run_until_complete(
                    run_workflow(context, task, "ticket")
                )
                if not ok and not self._stop_event.is_set():
                    loop.run_until_complete(
                        ticket_manager.add_comment_to_ticket(
                            task, f"Error:\n\n{message}"
                        )
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

    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep in small steps so the stop event can interrupt waits."""
        # Use wait to allow immediate wake-up on shutdown.
        self._stop_event.wait(timeout=seconds)
