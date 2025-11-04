from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.integrations.ticket_manager import TicketManager
from guildbotics.intelligences.common import AgentResponse, Labels
from guildbotics.intelligences.functions import identify_next_tasks, talk_as
from guildbotics.runtime.context import Context
from guildbotics.utils.git_tool import GitTool
from guildbotics.utils.i18n_tool import t


async def main(
    context: Context,
    messages: list[Message],
    git_tool: GitTool,
    ticket_manager: TicketManager,
):
    """
    Main function for ticket mode.
    Args:
        context (Context): The runtime context.
        messages (list[Message]): The conversation messages.
        git_tool (GitTool): The GitTool instance.
        ticket_manager (TicketManager): The TicketManager instance.
    Returns:
        AgentResponse: The agent response.
    """
    role = context.task.role if context.task.role else "professional"

    available_modes = Labels(Task.get_available_modes())
    next_task_response = await identify_next_tasks(
        context,
        role,
        git_tool.repo_path,
        messages,
        available_modes,
    )
    tasks = [nt.to_task() for nt in next_task_response.tasks]

    if context.task.owner:
        for task in tasks:
            task.owner = context.task.owner

    await ticket_manager.create_tickets(tasks)

    task_labels = [await ticket_manager.get_ticket_url(task) for task in tasks]
    system_message = t(
        "modes.ticket_mode.agent_response_message",
        task_labels=Labels(task_labels),
    )
    message = await talk_as(
        context,
        system_message,
        t("modes.ticket_mode.agent_response_context_location"),
        messages,
    )
    return AgentResponse(status=AgentResponse.DONE, message=message)
