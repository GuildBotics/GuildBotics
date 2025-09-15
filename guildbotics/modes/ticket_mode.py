from guildbotics.entities.message import Message
from guildbotics.entities.team import Service
from guildbotics.intelligences.common import AgentResponse, Labels
from guildbotics.intelligences.functions import identify_next_tasks, talk_as
from guildbotics.modes.mode_base import ModeBase
from guildbotics.runtime.context import Context
from guildbotics.utils.i18n_tool import t


class TicketMode(ModeBase):
    """
    Mode for handling ticket management systems.
    This mode allows creating and updating tickets based on identified tasks.
    It integrates with Git for version control and task management.
    It is primarily used for managing tasks and their dependencies in a project.
    """

    def __init__(self, context: Context):
        """
        Initialize the TicketMode.
        Args:
            context (Context): The context instance associated with this mode.
        """
        super().__init__(context)

    async def run(self, messages: list[Message]) -> AgentResponse:

        git_tool = await self.checkout()

        role = self.context.task.role if self.context.task.role else "professional"

        available_modes = ModeBase.get_available_modes(self.context.team)
        next_task_response = await identify_next_tasks(
            self.context,
            role,
            git_tool.repo_path,
            messages,
            available_modes,
        )
        tasks = [nt.to_task() for nt in next_task_response.tasks]

        if self.context.task.owner:
            for task in tasks:
                task.owner = self.context.task.owner

        ticket_manager = self.context.get_ticket_manager()
        await ticket_manager.create_tickets(tasks)

        task_labels = [await ticket_manager.get_ticket_url(task) for task in tasks]
        system_message = t(
            "modes.ticket_mode.agent_response_message",
            task_labels=Labels(task_labels),
        )
        message = await talk_as(
            self.context,
            system_message,
            t("modes.ticket_mode.agent_response_context_location"),
            messages,
        )
        return AgentResponse(status=AgentResponse.DONE, message=message)

    @classmethod
    def get_dependent_services(cls) -> list[Service]:
        """
        Get the list of services that this mode depends on.
        Returns:
            list[Service]: A list of service instances that this mode depends on.
        """
        return [Service.TICKET_MANAGER, Service.CODE_HOSTING_SERVICE]

    @classmethod
    def get_use_case_description(cls) -> str:
        """
        Get the use case description of the mode.
        Returns:
            str: A brief description of the mode's use case.
        """
        return t("modes.ticket_mode.use_case_description")
