from guildbotics.entities.message import Message
from guildbotics.entities.team import Service
from guildbotics.intelligences.common import AgentResponse
from guildbotics.intelligences.functions import reply_as
from guildbotics.modes.mode_base import ModeBase
from guildbotics.runtime.context import Context
from guildbotics.utils.i18n_tool import t


class CommentMode(ModeBase):
    """
    Mode for handling comments in ticket management systems.
    This mode allows adding comments to tickets and replying to chats.
    It is primarily used for communication and updates related to tasks.
    """

    def __init__(self, context: Context):
        """
        Initialize the CommentMode.
        Args:
            context (Context): The context instance associated with this mode.
        """
        super().__init__(context)

    async def run(self, messages: list[Message]) -> AgentResponse:

        git_tool = await self.checkout()
        message = await reply_as(self.context, messages, git_tool.repo_path)
        return AgentResponse(status=AgentResponse.ASKING, message=message)

    @classmethod
    def get_dependent_services(cls) -> list[Service]:
        """
        Get the list of services that this mode depends on.
        Returns:
            list[Service]: A list of service instances that this mode depends on.
        """
        return []

    @classmethod
    def get_use_case_description(cls) -> str:
        """
        Get the use case description of the mode.
        Returns:
            str: A brief description of the mode's use case.
        """
        return t("modes.comment_mode.use_case_description")
