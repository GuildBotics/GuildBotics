from abc import ABC, abstractmethod
from typing import Type, cast

from guildbotics.entities import Message
from guildbotics.entities.task import Task
from guildbotics.entities.team import Service, Team
from guildbotics.intelligences.common import AgentResponse, Labels
from guildbotics.intelligences.functions import talk_as
from guildbotics.runtime.context import Context
from guildbotics.utils.fileio import get_workspace_path
from guildbotics.utils.git_tool import GitTool
from guildbotics.utils.import_utils import instantiate_class, load_class


class ModeBase(ABC):
    """
    Abstract base class for modes.
    """

    def __init__(self, context: Context):
        """
        Initialize the Mode.
        Args:
            context (Context): The context instance associated with this mode.
        """
        self.context = context
        self.workspace_path = get_workspace_path(self.context.person.person_id)
        self.git_user = context.person.account_info.get("git_user", "Default User")
        self.git_email = context.person.account_info.get(
            "git_email", "default@example.com"
        )
        self.branch_name = f"ticket/{self.context.task.id}"
        self.code_hosting_service = self.context.get_code_hosting_service(
            self.context.task.repository
        )
        self.git_tool: GitTool | None = None

    @abstractmethod
    async def run(self, messages: list[Message]) -> AgentResponse:
        """
        Run the mode logic.
        Args:
            messages (list[Message]): The input messages for the mode.
        Returns:
            AgentResponse: The output of the mode.
        """
        pass

    @classmethod
    @abstractmethod
    def get_dependent_services(cls) -> list[Service]:
        """
        Get the list of services that this mode depends on.
        Returns:
            list[Service]: A list of service instances that this mode depends on.
        """
        pass

    @classmethod
    @abstractmethod
    def get_use_case_description(cls) -> str:
        """
        Get the use case description of the mode.
        Returns:
            str: A brief description of the mode's use case.
        """
        pass

    @staticmethod
    def _to_mode_cls_name(mode_cls: str | None) -> str:
        if not mode_cls:
            mode_cls = "comment"

        if "." not in mode_cls:
            pascal_case_name = "".join(
                part.capitalize() for part in mode_cls.split("_")
            )
            mode_cls = f"guildbotics.modes.{mode_cls}_mode.{pascal_case_name}Mode"
        return mode_cls

    @staticmethod
    def get_mode(context: Context) -> "ModeBase":
        """
        Get the mode for a workflow.
        If the mode is not specified, defaults to "comment" mode.
        Args:
            context: The Context instance for which to get the mode.
        Returns:
            An instance of the mode class specified in the workflow, or a default mode if none is specified.
        """
        mode_cls = ModeBase._to_mode_cls_name(context.task.mode)
        return instantiate_class(mode_cls, expected_type=ModeBase, context=context)

    @staticmethod
    def get_use_case_description_if_available(
        mode: str, available_services: list
    ) -> str | None:
        """
        Check if the mode is available based on the services it depends on.
        Args:
            mode (str): The mode class name to check.
            available_services (list): The list of available services in the workflow context.
        Returns:
            bool: True if the mode is available, False otherwise.
        """
        cls_obj = cast(Type[ModeBase], load_class(ModeBase._to_mode_cls_name(mode)))
        dependant_services = cls_obj.get_dependent_services()
        if not all(service in available_services for service in dependant_services):
            return None
        return cls_obj.get_use_case_description()

    @staticmethod
    def get_available_modes(team: Team) -> Labels:
        """
        Get the list of available modes.
        Returns:
            list[str]: A list of mode class names.
        """
        implemented_modes = ["comment", "edit", "ticket"]
        available_modes = {}

        available_services = team.project.get_available_services()
        for mode in implemented_modes:
            use_case_description = ModeBase.get_use_case_description_if_available(
                mode, available_services
            )
            if use_case_description:
                available_modes[mode] = use_case_description

        return Labels(available_modes)

    async def get_done_response(
        self,
        title: str,
        url: str,
        messages: list[Message],
        topic: str = "I have completed the task. Please review it.",
        context_location: str = "Ticket Comment",
    ) -> AgentResponse:
        """
        Create a done response for the mode.
        Args:
            title (str): The title to include in the done response.
            url (str): The URL to include in the done response.
        Returns:
            AgentResponse: The done response.
        """
        text = await talk_as(self.context, topic, context_location, messages)
        return AgentResponse(
            status=AgentResponse.DONE,
            message=f"{text}\n\n{Task.OUTPUT_PREFIX}[{title}]({url})",
        )

    async def get_git_tool(self) -> GitTool:
        """
        Get the git tool for the mode.
        Returns:
            GitTool: The git tool instance.
        """
        if self.git_tool is None:
            self.git_tool = GitTool(
                self.workspace_path,
                await self.code_hosting_service.get_repository_url(),
                self.context.logger,
                self.git_user,
                self.git_email,
                await self.code_hosting_service.get_default_branch(),
            )

        return self.git_tool

    async def checkout(self) -> GitTool:
        """Checkout the branch to the workspace."""
        git_tool = await self.get_git_tool()
        git_tool.checkout_branch(self.branch_name)
        return git_tool
