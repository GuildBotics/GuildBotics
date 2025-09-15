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

    def start(self):
        """
        Start the task scheduler.
        """
        threads = []
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
        # Wait on all threads (they run indefinitely)
        for thread in threads:
            thread.join()

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

        while True:
            start_time = datetime.datetime.now()
            self.context.logger.debug(
                f"Checking tasks at {start_time:%Y-%m-%d %H:%M:%S}."
            )

            # Run scheduled tasks
            for scheduled_task in scheduled_tasks:
                if scheduled_task.should_run(start_time):
                    loop.run_until_complete(
                        run_workflow(context, scheduled_task.task, "scheduled")
                    )
                sleep(1)

            # Check for tasks to work on
            task = loop.run_until_complete(ticket_manager.get_task_to_work_on())
            if task:
                ok, message = loop.run_until_complete(
                    run_workflow(context, task, "ticket")
                )
                if not ok:
                    loop.run_until_complete(
                        ticket_manager.add_comment_to_ticket(
                            task, f"Error:\n\n{message}"
                        )
                    )
                sleep(1)

            # Sleep until the next minute
            end_time = datetime.datetime.now()
            running_time = (end_time - start_time).total_seconds()
            sleep_sec = 60 - running_time
            if sleep_sec > 0:
                next_check_time = end_time + datetime.timedelta(seconds=sleep_sec)
                self.context.logger.debug(
                    f"Sleeping until {next_check_time:%Y-%m-%d %H:%M:%S}."
                )
                sleep(sleep_sec)
            self.last_checked = start_time
