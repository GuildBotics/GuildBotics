from guildbotics.entities.message import Message
from guildbotics.intelligences.common import AgentResponse
from guildbotics.intelligences.functions import reply_as
from guildbotics.runtime.context import Context
from guildbotics.utils.git_tool import GitTool


async def main(context: Context, messages: list[Message], git_tool: GitTool):
    message = await reply_as(context, messages, git_tool.repo_path)
    return AgentResponse(status=AgentResponse.ASKING, message=message)
