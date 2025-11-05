import shlex

from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.integrations.ticket_manager import TicketManager
from guildbotics.intelligences.common import AgentResponse, Labels
from guildbotics.intelligences.functions import identify_mode, identify_role, to_text
from guildbotics.runtime import Context
from guildbotics.templates.commands.workflows.modes.util import checkout
from guildbotics.utils.i18n_tool import t


async def _move_task_to_in_progress_if_ready(
    context: Context, ticket_manager: TicketManager
):
    """Move the task to 'In Progress' if it is ready."""
    if context.task.status == Task.READY and context.task.id is not None:
        await ticket_manager.move_ticket(context.task, Task.IN_PROGRESS)
        context.task.status = Task.IN_PROGRESS


async def _move_task_to_in_review_if_in_progress(
    context: Context, ticket_manager: TicketManager
):
    """Move the task to 'In Review' if it is currently 'In Progress'."""
    if context.task.status == Task.IN_PROGRESS:
        await ticket_manager.move_ticket(context.task, Task.IN_REVIEW)
        context.task.status = Task.IN_REVIEW


async def _build_task_error_message(context) -> str:
    error_text = t("drivers.task_scheduler.task_error")
    try:
        from guildbotics.intelligences.functions import talk_as

        talked_text = await talk_as(
            context,
            error_text,
            t("modes.ticket_mode.agent_response_context_location"),
            [],
        )

        return talked_text or error_text
    except Exception:
        return error_text


async def _main(context: Context, ticket_manager: TicketManager):
    # If the task is ready, move it to "In Progress".
    await _move_task_to_in_progress_if_ready(context, ticket_manager)

    # Prepare the input for the mode logic from the task details.
    messages = []
    title_and_description = t(
        "workflows.ticket_driven_workflow.title_and_description",
        title=context.task.title,
        description=context.task.description,
    )

    messages.append(
        Message(
            content=title_and_description,
            author=context.task.owner or "user",
            author_type=Message.USER,
            timestamp=(
                context.task.created_at.isoformat() if context.task.created_at else ""
            ),
        )
    )

    input = title_and_description
    if context.task.comments:
        input += t(
            "workflows.ticket_driven_workflow.comments",
            comments=to_text(context.task.comments),
        )
        for comment in context.task.comments:
            messages.append(comment)

    if not context.task.role:
        context.task.role = await identify_role(context, input)
        context.update_task(context.task)
        await ticket_manager.update_ticket(context.task)

    code_hosting_service = context.get_code_hosting_service(context.task.repository)

    if context.task.status == Task.RETROSPECTIVE:
        response = await context.invoke(
            "workflows/retrospective", code_hosting_service=code_hosting_service
        )
    else:
        git_tool = await checkout(context)
        params = {
            "messages": messages,
            "git_tool": git_tool,
            "code_hosting_service": code_hosting_service,
            "ticket_manager": ticket_manager,
        }

        if _is_custom_command(context, messages):
            response = await _invoke_custom_command(context, params)
        else:
            if not context.task.mode:
                available_modes = Labels(Task.get_available_modes())
                context.task.mode = await identify_mode(
                    context, available_modes, input
                )
                await ticket_manager.update_ticket(context.task)

            command_name = _mode_to_command_name(context.task.mode)
            response = await context.invoke(command_name, **params)

    # If the response is asking for more information, return it.
    if not response.skip_ticket_comment:
        await ticket_manager.add_comment_to_ticket(context.task, response.message)
    if response.status == response.ASKING:
        return

    # If the task is in progress, move it to "In Review".
    await _move_task_to_in_review_if_in_progress(context, ticket_manager)


async def _invoke_custom_command(context: Context, params: dict) -> AgentResponse:
    """
    Invoke a custom command based on the last message.
    Args:
        context (Context): The runtime context.
        params (dict): The parameters to pass to the command.
    Returns:
        AgentResponse: The agent response.
    """
    content = _get_last_message(context, params["messages"])
    lines = content.splitlines()
    command_name, command_args = _preprocess_line(lines[0])
    if len(lines) > 1:
        context.pipe = "\n".join(lines[1:]).strip()

    response = await context.invoke(command_name, *command_args, **params)
    if isinstance(response, AgentResponse):
        return response

    message = str(response) if response is not None else ""
    return AgentResponse(status=AgentResponse.DONE, message=message)


def _preprocess_line(line: str) -> tuple[str, list[str]]:
    """
    Preprocess a command line by extracting the command name and arguments.
    Args:
        line (str): The command line to preprocess.
    Returns:
        tuple[str, list[str]]: The command name and a list of arguments.
    """
    try:
        words = shlex.split(line[2:].strip())
    except ValueError:
        words = line[2:].strip().split()

    return words[0], words[1:]


def _get_last_message(context: Context, messages: list[Message]) -> str:
    """
    Get the content of the last message.
    Args:
        messages (list[Message]): The list of messages.
    Returns:
        str: The content of the last message.
    """
    if len(messages) == 1:
        return context.task.description
    else:
        return messages[-1].content.strip()


def _is_custom_command(context: Context, messages: list[Message]) -> bool:
    """
    Determine if the last message is a custom command.
    Args:
        messages (list[Message]): The list of messages.
    Returns:
        bool: True if the last message is a custom command, False otherwise.
    """
    if not messages:
        return False

    return _get_last_message(context, messages).startswith("//")


def _mode_to_command_name(mode: str | None) -> str:
    """
    Convert a mode name to a command name.
    Args:
        mode (str | None): The mode name.
    Returns:
        str: The command name.
    """
    if not mode:
        mode = "comment"

    return f"workflows/modes/{mode}_mode"


async def main(context: Context):
    """
    Main function for the ticket-driven workflow.
    Args:
        context (Context): The runtime context.
    """
    ticket_manager = context.get_ticket_manager()
    task = await ticket_manager.get_task_to_work_on()
    if task is None:
        return
    context.update_task(task)
    try:
        await _main(context, ticket_manager)
    except Exception:
        message = await _build_task_error_message(context)
        await ticket_manager.add_comment_to_ticket(task, message)
        raise
