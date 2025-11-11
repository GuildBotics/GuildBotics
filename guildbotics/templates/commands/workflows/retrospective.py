from typing import Any

from guildbotics.entities.message import Message
from guildbotics.integrations.code_hosting_service import CodeHostingService
from guildbotics.intelligences.common import (
    AgentResponse,
    ArtifactProcessEvaluation,
    ImprovementRecommendations,
    RootCauseAnalysis,
)
from guildbotics.intelligences.functions import get_content, talk_as
from guildbotics.runtime.context import Context
from guildbotics.templates.commands.workflows.modes.edit_mode import (
    find_pr_url_from_task_comments,
    pr_to_text,
)
from guildbotics.utils.i18n_tool import t


async def evaluate_interaction_performance(
    context: Context,
    interaction_text: str,
    **retrospective_params: Any,
) -> str:
    """
    Evaluate performance for a pull request-like interaction.

    Keyword overrides enable reuse of the same prompt for other interaction
    types (e.g., design reviews, incidents) by customizing section labels and
    subject metadata.
    """
    lang = context.team.project.get_language_name()
    default_params = {
        "subject_type": t("intelligences.functions.subject_type"),
        "summary_label": t("intelligences.functions.summary_label"),
        "feedback_label": t("intelligences.functions.feedback_label"),
        "outcome_label": t("intelligences.functions.outcome_label"),
        "positive_outcome_value": t("intelligences.functions.positive_outcome_value"),
        "negative_outcome_value": t("intelligences.functions.negative_outcome_value"),
    }

    merged_params = {**default_params, **retrospective_params}
    evaluation: ArtifactProcessEvaluation = await get_content(
        context,
        "functions/evaluate_interaction_performance",
        message=interaction_text,
        params={"language": lang, **merged_params},
    )

    return t(
        "commands.workflows.modes.edit_mode.pull_request_performance_evaluation",
        score=int(evaluation.overall_score * 100),
        reason=evaluation.reason,
        review_comment_count=evaluation.review_comment_count,
        review_cycle_count=evaluation.review_cycle_count,
        request_changes_count=evaluation.request_changes_count,
        review_sentiment_score=evaluation.review_sentiment_score,
        context=evaluation.context,
    )


async def analyze_root_cause(
    context: Context,
    interaction_text: str,
    evaluation: str,
    *,
    evaluation_header_label: str | None = None,
    feedback_header_label: str | None = None,
    subject_type: str | None = None,
) -> RootCauseAnalysis:
    """
    Analyze root causes for an interaction with optional label overrides.
    """
    if evaluation_header_label is None and feedback_header_label is None:
        message = t(
            "commands.workflows.modes.edit_mode.analyze_pr_root_cause",
            evaluation=evaluation,
            pr_text=interaction_text,
        )
    else:
        eval_label = evaluation_header_label or "Evaluation Result"
        fb_label = feedback_header_label or "Original Feedback"
        message = (
            f"# {eval_label}\n{evaluation}\n---\n# {fb_label}\n{interaction_text}\n"
        )

    lang = context.team.project.get_language_name()
    session_state = {"language": lang}
    if subject_type:
        session_state["subject_type"] = subject_type
    else:
        session_state["subject_type"] = t("intelligences.functions.subject_type")

    result: RootCauseAnalysis = await get_content(
        context,
        "functions/analyze_root_cause",
        message=message,
        params=session_state,
    )
    return result


async def propose_process_improvements(
    context: Context,
    root_cause_analysis: RootCauseAnalysis,
    *,
    subject_type: str | None = None,
) -> ImprovementRecommendations:
    """
    Turn root-cause analysis into concrete improvement suggestions.
    """
    lang = context.team.project.get_language_name()
    session_state = {"language": lang}
    if subject_type:
        session_state["subject_type"] = subject_type
    else:
        session_state["subject_type"] = t("intelligences.functions.subject_type")

    message = f"# RootCauseAnalysis:\n{str(root_cause_analysis)}\n"
    result: ImprovementRecommendations = await get_content(
        context,
        "functions/propose_process_improvements",
        message=message,
        params=session_state,
    )
    return result


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
        "commands.workflows.modes.edit_mode.evaluation_and_root_cause",
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
        t("commands.workflows.modes.edit_mode.evaluation_topic"),
        context_location=t("commands.workflows.modes.edit_mode.evaluation_context_location"),
        conversation_history=evaluation_messages,
    )
    return AgentResponse(
        status=AgentResponse.ASKING,
        message=evaluation_and_root_cause + "\n\n---\n\n" + result,
    )
