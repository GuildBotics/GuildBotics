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
from guildbotics.workflows import WorkflowBase


class TicketDrivenWorkflow(WorkflowBase):

    def __init__(self, context: Context):
        super().__init__(context=context)

    async def move_task_to_in_progress_if_ready(self):
        """Move the task to 'In Progress' if it is ready."""
        if self.context.task.status == Task.READY and self.context.task.id is not None:
            await self.context.get_ticket_manager().move_ticket(
                self.context.task, Task.IN_PROGRESS
            )
            self.context.task.status = Task.IN_PROGRESS

    async def move_task_to_in_review_if_in_progress(self):
        """Move the task to 'In Review' if it is currently 'In Progress'."""
        if self.context.task.status == Task.IN_PROGRESS:
            await self.context.get_ticket_manager().move_ticket(
                self.context.task, Task.IN_REVIEW
            )
            self.context.task.status = Task.IN_REVIEW

    async def run(self):
        ticket_manager = self.context.get_ticket_manager()

        # If the task is ready, move it to "In Progress".
        await self.move_task_to_in_progress_if_ready()

        # Prepare the input for the mode logic from the task details.
        messages = []
        title_and_description = t(
            "workflows.ticket_driven_workflow.title_and_description",
            title=self.context.task.title,
            description=self.context.task.description,
        )

        messages.append(
            Message(
                content=title_and_description,
                author=self.context.task.owner or "user",
                author_type=Message.USER,
                timestamp=(
                    self.context.task.created_at.isoformat()
                    if self.context.task.created_at
                    else ""
                ),
            )
        )

        input = title_and_description
        if self.context.task.comments:
            input += t(
                "workflows.ticket_driven_workflow.comments",
                comments=to_text(self.context.task.comments),
            )
            for comment in self.context.task.comments:
                messages.append(comment)

        if not self.context.task.role:
            self.context.task.role = await identify_role(self.context, input)
            self.context.update_task(self.context.task)
            await ticket_manager.update_ticket(self.context.task)

        if not self.context.task.mode:
            available_modes = ModeBase.get_available_modes(self.context.team)
            self.context.task.mode = await identify_mode(
                self.context, available_modes, input
            )
            await ticket_manager.update_ticket(self.context.task)

        # Run the mode logic
        messages[-1].content = preprocess(self.context, messages[-1].content)
        response = await ModeBase.get_mode(self.context).run(messages)

        # If the response is asking for more information, return it.
        if not response.skip_ticket_comment:
            await ticket_manager.add_comment_to_ticket(
                self.context.task, response.message
            )
        if response.status == response.ASKING:
            return

        # If the task is in progress, move it to "In Review".
        await self.move_task_to_in_review_if_in_progress()
