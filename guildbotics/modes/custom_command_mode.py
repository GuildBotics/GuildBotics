import shlex

from guildbotics.entities.message import Message
from guildbotics.entities.team import Service
from guildbotics.intelligences.common import AgentResponse
from guildbotics.modes.mode_base import ModeBase
from guildbotics.runtime.context import Context


class CustomCommandMode(ModeBase):
    def __init__(self, context: Context):
        super().__init__(context)

    async def run(self, messages: list[Message]) -> AgentResponse:
        content = self.get_last_message(messages)
        lines = content.splitlines()
        command_name, command_args = self._preprocess_line(lines[0])
        if len(lines) > 1:
            self.context.pipe = "\n".join(lines[1:]).strip()

        response = await self.context.invoke(
            command_name, *command_args, messages=messages
        )
        if isinstance(response, AgentResponse):
            return response

        message = str(response) if response is not None else ""
        return AgentResponse(status=AgentResponse.DONE, message=message)

    def _preprocess_line(self, line: str) -> tuple[str, list[str]]:
        try:
            words = shlex.split(line[2:].strip())
        except ValueError:
            words = line[2:].strip().split()

        return words[0], words[1:]

    @classmethod
    def get_dependent_services(cls) -> list[Service]:
        return []

    @classmethod
    def get_use_case_description(cls) -> str:
        return (
            "A mode that allows the agent to execute custom commands "
            "prefixed with '//' to perform specific tasks."
        )

    @staticmethod
    def get_last_message(messages: list[Message]) -> str:
        return messages[-1].content.strip()

    @staticmethod
    def is_custom_command(messages: list[Message]) -> bool:
        if not messages:
            return False
        return CustomCommandMode.get_last_message(messages).startswith("//")
