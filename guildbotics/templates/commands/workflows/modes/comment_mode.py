from guildbotics.entities.message import Message
from guildbotics.intelligences.common import AgentResponse
from guildbotics.intelligences.functions import reply_as
from guildbotics.runtime.context import Context
from guildbotics.utils.git_tool import GitTool


async def main(context: Context, messages: list[Message], git_tool: GitTool):
    """
    Main function for comment mode.
    Args:
        context (Context): The runtime context.
        messages (list[Message]): The conversation messages.
        git_tool (GitTool): The GitTool instance.
    Returns:
        AgentResponse: The agent response.
    """
    message = await reply_as(context, messages, git_tool.repo_path)
    return AgentResponse(status=AgentResponse.ASKING, message=message)
