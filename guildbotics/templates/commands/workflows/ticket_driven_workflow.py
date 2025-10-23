from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.intelligences.functions import (
    identify_mode,
    identify_role,
    preprocess,
    to_text,
)
from guildbotics.modes.mode_base import ModeBase
from guildbotics.runtime import Context
from guildbotics.utils.i18n_tool import t


async def move_task_to_in_progress_if_ready(context: Context):
    """Move the task to 'In Progress' if it is ready."""
    if context.task.status == Task.READY and context.task.id is not None:
        await context.get_ticket_manager().move_ticket(context.task, Task.IN_PROGRESS)
        context.task.status = Task.IN_PROGRESS


async def move_task_to_in_review_if_in_progress(context: Context):
    """Move the task to 'In Review' if it is currently 'In Progress'."""
    if context.task.status == Task.IN_PROGRESS:
        await context.get_ticket_manager().move_ticket(context.task, Task.IN_REVIEW)
        context.task.status = Task.IN_REVIEW


async def main(context: Context):
    ticket_manager = context.get_ticket_manager()

    # If the task is ready, move it to "In Progress".
    await move_task_to_in_progress_if_ready(context)

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

    if not context.task.mode:
        available_modes = ModeBase.get_available_modes(context.team)
        context.task.mode = await identify_mode(context, available_modes, input)
        await ticket_manager.update_ticket(context.task)

    # Run the mode logic
    messages[-1].content = preprocess(context, messages[-1].content)
    response = await ModeBase.get_mode(context).run(messages)

    # If the response is asking for more information, return it.
    if not response.skip_ticket_comment:
        await ticket_manager.add_comment_to_ticket(context.task, response.message)
    if response.status == response.ASKING:
        return

    # If the task is in progress, move it to "In Review".
    await move_task_to_in_review_if_in_progress(context)
