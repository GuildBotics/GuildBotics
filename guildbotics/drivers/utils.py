import datetime
import traceback

from guildbotics.entities import Task
from guildbotics.runtime import Context
from guildbotics.utils.fileio import get_storage_path
from guildbotics.utils.import_utils import instantiate_class
from guildbotics.workflows import WorkflowBase


def _to_workflow(context: Context, task: Task) -> WorkflowBase:
    """
    Convert a Task to a Workflow.
    """

    module_and_cls = task.workflow

    if "." not in module_and_cls:
        pascal_case_name = "".join(
            part.capitalize() for part in task.workflow.split("_")
        )
        module_and_cls = (
            f"guildbotics.workflows.{task.workflow}_workflow.{pascal_case_name}Workflow"
        )

    return instantiate_class(
        module_and_cls, expected_type=WorkflowBase, context=context
    )


async def run_workflow(
    context: Context,
    task: Task,
    task_type: str,
) -> bool:
    """Run a workflow either in‐process or in a separate virtual environment.

    By default, the workflow executes in the current Python process.  To isolate
    dependencies you can set `task.use_subprocess=True` and optionally supply
    `task.venv_path`.

    Args:
        context: The workflow execution context.
        task: The Task to execute. Its `use_subprocess` and `venv_path` fields control subprocess behavior.
        task_type: A label for logging (e.g., "scheduled" or "todo list").

    Returns:
        True if the workflow ran without error, False otherwise.
    """
    try:
        start_time = datetime.datetime.now()
        context.update_task(task)
        person = context.person
        context.logger.info(
            f"Running {task_type} task '{task.title}' for person '{person.person_id}'..."
        )

        workflow = _to_workflow(context, task)
        await workflow.run()

        end_time = datetime.datetime.now()
        duration = (end_time - start_time).total_seconds()
        context.logger.info(
            f"Finished running {task_type} task '{task.title}' for person "
            f"'{person.person_id}' in {duration:.2f}s"
        )
        return True
    except Exception as e:
        context.logger.error(
            f"Error running workflow for task '{task.title}' for person "
            f"'{person.person_id}': {e}"
        )
        error_message = traceback.format_exc()
        context.logger.error(error_message)
        write_error_log(context, error_message)
        return False


def write_error_log(context: Context, message: str) -> None:
    """Write an error message to the log file."""
    try:
        log_file_path = get_storage_path() / "error.log"
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception:
        context.logger.error("Failed to write to error log file.")
        context.logger.error(traceback.format_exc())
