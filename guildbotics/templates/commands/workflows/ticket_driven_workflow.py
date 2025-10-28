from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.integrations.ticket_manager import TicketManager
from guildbotics.intelligences.functions import identify_mode, identify_role, to_text
from guildbotics.modes.custom_command_mode import CustomCommandMode
from guildbotics.modes.mode_base import ModeBase
from guildbotics.runtime import Context
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

    if CustomCommandMode.is_custom_command(messages):
        response = await CustomCommandMode(context).run(messages)
    else:
        if not context.task.mode:
            available_modes = ModeBase.get_available_modes(context.team)
            context.task.mode = await identify_mode(context, available_modes, input)
            await ticket_manager.update_ticket(context.task)

        response = await ModeBase.get_mode(context).run(messages)

    # If the response is asking for more information, return it.
    if not response.skip_ticket_comment:
        await ticket_manager.add_comment_to_ticket(context.task, response.message)
    if response.status == response.ASKING:
        return

    # If the task is in progress, move it to "In Review".
    await _move_task_to_in_review_if_in_progress(context, ticket_manager)


async def main(context: Context):
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
