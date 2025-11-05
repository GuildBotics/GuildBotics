from guildbotics.entities.message import Message
from guildbotics.integrations.code_hosting_service import CodeHostingService
from guildbotics.intelligences.common import AgentResponse
from guildbotics.intelligences.functions import (
    analyze_root_cause,
    evaluate_interaction_performance,
    propose_process_improvements,
    talk_as,
)
from guildbotics.runtime.context import Context
from guildbotics.templates.commands.workflows.modes.edit_mode import (
    find_pr_url_from_task_comments,
    pr_to_text,
)
from guildbotics.utils.i18n_tool import t


async def main(
    context: Context, code_hosting_service: CodeHostingService
) -> AgentResponse:
    """
    Handle the retrospective flow for workflows.
    Args:
        context (Context): The runtime context.
        code_hosting_service (CodeHostingService): The code hosting service integration.
    Returns:
        AgentResponse: The agent response containing the evaluation and proposed improvements.
    """
    pull_request_url = find_pr_url_from_task_comments(context.task, True)
    pr = await code_hosting_service.get_pull_request(pull_request_url)
    pr_text = pr_to_text(pr)
    evaluation = await evaluate_interaction_performance(context, pr_text)
    root_cause = await analyze_root_cause(context, pr_text, evaluation)
    proposal = await propose_process_improvements(context, root_cause)
    ticket_manager = context.get_ticket_manager()

    suggestions = sorted(proposal.suggestions)
    if len(suggestions) > 5:
        suggestions = suggestions[:5]
    tasks = [suggestion.to_task() for suggestion in suggestions]
    await ticket_manager.create_tickets(tasks)

    evaluation_and_root_cause = t(
        "modes.edit_mode.evaluation_and_root_cause",
        evaluation=evaluation,
        root_cause=str(root_cause),
    )
    evaluation_messages = [
        Message(
            content=evaluation_and_root_cause,
            author="Evaluation System",
            author_type=Message.USER,
            timestamp="",
        ),
    ]

    result = await talk_as(
        context,
        t("modes.edit_mode.evaluation_topic"),
        context_location=t("modes.edit_mode.evaluation_context_location"),
        conversation_history=evaluation_messages,
    )
    return AgentResponse(
        status=AgentResponse.ASKING,
        message=evaluation_and_root_cause + "\n\n---\n\n" + result,
    )
