import datetime
import shlex
import traceback

from guildbotics.drivers.command_runner import CommandRunner
from guildbotics.runtime import Context


async def run_command(context: Context, command: str, task_type: str) -> bool:
    """
    Run a command within the given context and log its execution.
    Args:
        context (Context): The execution context.
        command (str): The command to run.
        task_type (str): The type of task being executed (for logging purposes).
    Returns:
        bool: True if the command ran successfully, False otherwise.
    """
    try:
        start_time = datetime.datetime.now()
        person = context.person
        context.logger.info(
            f"Running {task_type} command '{command}' for person '{person.person_id}'..."
        )

        words = shlex.split(command)
        await CommandRunner(context, words[0], words[1:]).run()

        end_time = datetime.datetime.now()
        duration = (end_time - start_time).total_seconds()
        context.logger.info(
            f"Finished running {task_type} command '{command}' for person "
            f"'{person.person_id}' in {duration:.2f}s"
        )
        return True
    except Exception as e:
        context.logger.error(
            f"Error running {task_type} command '{command}' for person "
            f"'{person.person_id}': {e}"
        )
        context.logger.error(traceback.format_exc())
        return False
