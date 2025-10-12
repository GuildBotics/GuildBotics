import io
import json
import logging
import shlex
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, TypeVar

import yaml
from pydantic import BaseModel

from guildbotics.entities.message import Message
from guildbotics.intelligences.common import (
    AgentResponse,
    ArtifactProcessEvaluation,
    DecisionResponse,
    ImprovementRecommendations,
    Labels,
    MessageResponse,
    NextTasksResponse,
    RootCauseAnalysis,
)
from guildbotics.runtime import Context
from guildbotics.utils.fileio import get_prompt_path, load_markdown_with_frontmatter
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.text_utils import get_body_from_prompt

TBaseModel = TypeVar("TBaseModel", bound=BaseModel)


def to_text(obj: dict | list[dict] | TBaseModel | list[TBaseModel]) -> str:
    """
    Convert a Pydantic model, its subclass, or a list of them to a text representation.

    Args:
        obj (dict | list[dict] | TBaseModel | list[TBaseModel]): The object to convert.

    Returns:
        str: The text representation of the object.
    """

    def _clean(item: dict | BaseModel) -> dict:
        """Clean a single item by dumping BaseModel and removing empty values."""
        data = item.model_dump() if isinstance(item, BaseModel) else item
        return {k: v for k, v in data.items() if v not in ["", [], None]}

    # Normalize to list and track if original was single
    if isinstance(obj, (BaseModel, dict, list)):
        single = not isinstance(obj, list)
        items = [obj] if single else obj  # type: ignore[list-item]
        cleaned = [_clean(item) for item in items]  # type: ignore[arg-type]
        to_dump = cleaned[0] if single else cleaned
        return yaml.dump(to_dump, default_flow_style=False, allow_unicode=True).strip()
    # Fallback for other types
    return str(obj).strip()


def messages_to_json(messages: list[Message]) -> str:
    """
    Convert a list of Message objects to JSON-compatible format.
    Args:
        messages (list[Message]): The list of Message objects to convert.
    Returns:
        dict: The JSON-compatible representation of the message.
    """
    message_list = [
        {
            "content": message.content,
            "author": message.author,
            "author_type": message.author_type,
        }
        for message in messages
    ]
    return json.dumps(message_list, ensure_ascii=False, indent=2)


def messages_to_simple_dicts(messages: list[Message]) -> list[dict]:
    """
    Convert the inputs to a simple dictionary format.
    Args:
        inputs (list[Message]): The list of messages to convert.
    Returns:
        dict: A dictionary representation of the inputs.
    """
    inputs: list[dict] = []
    for message in messages:
        inputs.append(message.to_simple_dict())
    return inputs


def to_dict(
    context: Context,
    params: dict | None = None,
    cwd: Path | None = None,
    response_model: type[TBaseModel] | None = None,
) -> dict[str, Any]:
    kwargs: dict = {}
    if params is not None:
        params["context"] = context
        now = datetime.now().astimezone()
        if "now" not in params:
            params["now"] = now.strftime("%Y-%m-%d %H:%M")
        if "today" not in params:
            params["today"] = now.strftime("%Y-%m-%d")
        kwargs["session_state"] = params
        kwargs["add_state_in_messages"] = True
    if cwd:
        kwargs["cwd"] = cwd
    if response_model:
        kwargs["response_model"] = response_model
    return kwargs


async def _get_content(
    context: Context,
    name: str,
    message: str,
    params: dict | None = None,
    cwd: Path | None = None,
    response_model: type[TBaseModel] | None = None,
) -> Any:
    brain = context.get_brain(name)
    kwargs = to_dict(context, params, cwd, response_model)
    return await brain.run(message=message, **kwargs)


async def convert_object(
    context: Context,
    message: str,
    response_model: type[TBaseModel],
) -> TBaseModel:
    """
    Convert a message to the specified object based on a JSON schema.

    Args:
        message (str): The message to convert.
        response_model (TypeVar, optional): The Pydantic model class to convert to.

    Returns:
        TBaseModel: The converted object.
    """
    return await _get_content(
        context,
        "functions/convert_object",
        message=message,
        response_model=response_model,
    )


async def get_content(
    context: Context,
    name: str,
    message: str,
    params: dict | None = None,
    cwd: Path | None = None,
) -> Any:
    brain = context.get_brain(name)
    kwargs = to_dict(context, params, cwd)
    output = await brain.run(message=message, **kwargs)

    if not brain.response_class:
        return output

    if isinstance(output, brain.response_class):
        return output

    return await convert_object(context, output, brain.response_class)


async def talk_as(
    context: Context,
    topic: str,
    context_location: str,
    conversation_history: list[Message],
) -> str:
    """
    To talk a message as the character about the specified topic or content.
    Args:
        topic (str): The topic or content to talk about.
        context_location (str): The location or context of the conversation.
        conversation_history (list[Message]): The history of the conversation.
    Returns:
        str: The generated response text.
    """
    session_state = {"topic": topic}
    if context_location:
        session_state["context_location"] = context_location
    if conversation_history:
        session_state["conversation_history"] = messages_to_json(conversation_history)

    reply: MessageResponse = await get_content(
        context,
        "functions/talk_as",
        message="",
        params=session_state,
    )
    return reply.content.strip() if reply else ""


async def reply_as(
    context: Context,
    messages: list[Message],
    cwd: Path,
    context_type: str = "",
    message_type: str = "",
) -> str:
    session_state = {
        "context_type": context_type or t("intelligences.functions.context_type"),
        "message_type": message_type or t("intelligences.functions.message_type"),
    }
    reply: MessageResponse = await get_content(
        context,
        "functions/reply_as",
        message=messages_to_json(messages),
        params=session_state,
        cwd=cwd,
    )
    return reply.content.strip() if reply else ""


async def identify_role(context: Context, input: str) -> str:
    """Identify the role based on the input text."""
    roles: dict[str, str] = {
        role.id: role.summary for role in context.person.roles.values()
    }
    session_state = {"item_type": "role", "candidates": str(Labels(roles))}
    response: DecisionResponse = await get_content(
        context,
        "functions/identify_item",
        message=input,
        params=session_state,
    )
    return response.label


async def identify_mode(context: Context, available_modes: Labels, input: str) -> str:
    """Identify the mode based on the input text."""
    session_state = {"item_type": "mode", "candidates": str(available_modes)}
    response: DecisionResponse = await get_content(
        context,
        "functions/identify_item",
        message=input,
        params=session_state,
    )
    return response.label


async def write_commit_message(context: Context, task_title: str, changes: str) -> str:
    params = {"task_title": task_title, "changes": changes}

    return await get_content(
        context,
        "functions/write_commit_message",
        message="",
        params=params,
    )


async def evaluate_interaction_performance(
    context: Context,
    interaction_text: str,
    **retrospective_params: Any,
) -> str:
    """
    Evaluate performance for a pull request-like interaction.

    This function supports parameterization via keyword arguments to generalize
    the retrospective flow beyond Pull Requests. Callers can provide optional
    labels and subject information which are forwarded to the underlying
    prompting template. Supported optional params include:

    - subject_type: The artifact/interaction type label (e.g., "Pull Request", "Design Review").
    - summary_label: Section label used in the input text to denote the overview/summary.
    - feedback_label: Section label used to denote review/feedback comments.
    - outcome_label: Section label used to denote the final outcome.
    - positive_outcome_value / negative_outcome_value: Strings indicating success/failure.

    Args:
        interaction_text (str): Source text that contains the sections to evaluate.
        retrospective_params: Optional keyword parameters to override defaults.

    Returns:
        str: A localized, human-readable evaluation summary.
    """
    lang = context.team.project.get_language_name()
    # Localized defaults for PR retrospectives (moved to locale files)
    default_params = {
        "subject_type": t("intelligences.functions.subject_type"),
        "summary_label": t("intelligences.functions.summary_label"),
        "feedback_label": t("intelligences.functions.feedback_label"),
        "outcome_label": t("intelligences.functions.outcome_label"),
        "positive_outcome_value": t("intelligences.functions.positive_outcome_value"),
        "negative_outcome_value": t("intelligences.functions.negative_outcome_value"),
    }

    merged_params = {**default_params, **retrospective_params}
    params = {"language": lang, **merged_params}

    evaluation: ArtifactProcessEvaluation = await get_content(
        context,
        "functions/evaluate_interaction_performance",
        message=interaction_text,
        params=params,
    )

    return t(
        "modes.edit_mode.pull_request_performance_evaluation",
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
    Analyze root causes for a PR-like interaction, with optional parameterization.

    Callers may override default section headers and subject type to reuse the
    same analysis flow for other interaction types.

    Args:
        interaction_text (str): Original feedback/conversation text.
        evaluation (str): A human-readable evaluation summary.
        evaluation_header_label (str | None): Header label for the evaluation section.
        feedback_header_label (str | None): Header label for the original feedback section.
        subject_type (str | None): Artifact/interaction type label.

    Returns:
        RootCauseAnalysis: Structured root cause analysis result.
    """
    # Build the analysis prompt with either defaults (PR) or caller-provided labels.
    if evaluation_header_label is None and feedback_header_label is None:
        message = t(
            "modes.edit_mode.analyze_pr_root_cause",
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
        # Default subject type localized via locale files
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
    Propose concrete, actionable improvements based on root cause analysis.

    Optional subject_type enables generalization beyond PRs (e.g., design review,
    incident response, support thread).
    """
    message = f"# RootCauseAnalysis:\n{str(root_cause_analysis)}\n"

    lang = context.team.project.get_language_name()
    session_state = {"language": lang}
    if subject_type:
        session_state["subject_type"] = subject_type
    else:
        session_state["subject_type"] = t("intelligences.functions.subject_type")

    result: ImprovementRecommendations = await get_content(
        context,
        "functions/propose_process_improvements",
        message=message,
        params=session_state,
    )

    return result


async def write_pull_request_description(
    context: Context,
    changes: str,
    commit_message: str,
    ticket_url: str,
    pr_template: str,
) -> str:
    task = context.task
    ticket_title = task.title
    ticket_description = task.description
    git_diff = changes
    commit_comments = commit_message

    params = {
        "ticket_url": ticket_url,
        "ticket_title": ticket_title,
        "ticket_description": ticket_description,
        "git_diff": git_diff,
        "commit_comments": commit_comments,
        "pr_template": pr_template,
    }

    return await get_content(
        context,
        "functions/write_pull_request_description",
        message="",
        params=params,
    )


async def identify_next_tasks(
    context: Context,
    role_id: str,
    cwd: Path,
    messages: list[Message],
    available_modes: Labels,
) -> NextTasksResponse:
    """
    Identify and list the next tasks for a given role.

    Args:
        role_id (str): The ID of the role to identify tasks for.
        cwd (Path): The current working directory.
        messages (list[Message]): The conversation messages to analyze.
        available_modes (Labels): The available modes for task identification.

    Returns:
        NextTasksResponse: Structured list of next tasks with explanations.
    """
    conversation = json.dumps(
        messages_to_simple_dicts(messages), ensure_ascii=False, indent=2
    )
    available_modes.indent = 4
    session_state = {
        "role": str(context.person.get_role(role_id)),
        "available_modes": str(available_modes),
        "language": context.team.project.get_language_name(),
    }

    return await get_content(
        context,
        "functions/identify_next_tasks",
        message=conversation,
        params=session_state,
        cwd=cwd,
    )


async def identify_output_type(context: Context, input: str) -> str:
    """Identify the output type based on the input text."""
    available_output_types = {
        "code": "Code writing",
        "markdown": "Documentation writing",
    }
    session_state = {
        "item_type": "output type",
        "candidates": str(Labels(available_output_types)),
    }
    response: DecisionResponse = await get_content(
        context,
        "functions/identify_item",
        message=input,
        params=session_state,
    )
    return response.label


async def identify_message_type(context: Context, input: str) -> str:
    available_message_types = {
        "error": "Internal Error Message",
        "normal": "Normal Message",
    }
    session_state = {
        "item_type": "message type",
        "candidates": str(Labels(available_message_types)),
    }
    response: DecisionResponse = await get_content(
        context,
        "functions/identify_item",
        message=input,
        params=session_state,
    )
    return response.label


async def identify_pr_comment_action(context: Context, input: str) -> str:
    """Identify whether a PR comment requests edits or just acknowledgment.

    Returns one of:
      - "edit": The comment requires code/document changes.
      - "ack": No edits needed (e.g., thanks, LGTM, bot command).
    """
    available_actions = {
        "edit": "Requires file edits or fixes",
        "ack": "No edits required (thanks, LGTM, bot command)",
    }
    session_state = {
        "item_type": "review action",
        "candidates": str(Labels(available_actions)),
    }
    response: DecisionResponse = await get_content(
        context,
        "functions/identify_item",
        message=input,
        params=session_state,
    )
    return response.label


async def analyze_log(context: Context, log_output: str) -> AgentResponse:
    """
    Analyze the log output and return an AgentResponse.
    Args:
        log_output (str): The log output to analyze.
    Returns:
        AgentResponse: The response containing the analysis of the log output.
    """
    response: AgentResponse = await get_content(
        context, "functions/analyze_log", message=log_output
    )
    return response


@contextmanager
def capture_logs(context: Context, level: int = logging.ERROR) -> Iterator[io.StringIO]:
    log_buffer = io.StringIO()
    buffer_handler = logging.StreamHandler(log_buffer)
    buffer_handler.setLevel(level)  # Capture only logs at or above this level
    buffer_handler.setFormatter(logging.Formatter("%(message)s"))
    context.logger.addHandler(buffer_handler)
    try:
        yield log_buffer
    finally:
        context.logger.removeHandler(buffer_handler)
        buffer_handler.close()
        log_buffer.close()


async def to_agent_response(
    context: Context, json_str: str | AgentResponse, log_output: str
) -> AgentResponse:
    if isinstance(json_str, AgentResponse):
        return json_str

    try:
        return AgentResponse.model_validate_json(json_str)
    except Exception as e:
        if log_output:
            message_type = await identify_message_type(context, log_output)
            if message_type == "error":
                message = await analyze_log(context, log_output)
                return AgentResponse(
                    status=AgentResponse.ASKING,
                    message=f"{message}",
                )
        return await convert_object(context, json_str, AgentResponse)


async def edit_files(context: Context, input: list[dict], cwd: Path) -> AgentResponse:
    """
    Edit files using the intelligence service.
    Args:
        input (str): The input for the editing operation.
        cwd (Path): The current working directory where the files are located.
    Returns:
        AgentResponse: The response from the editing operation.
    """
    input_text = json.dumps(input, ensure_ascii=False, indent=2)

    log_output = ""
    with capture_logs(context) as log_buffer:
        response = await _get_content(
            context,
            "functions/edit_files",
            message=input_text,
            params={},
            cwd=cwd,
        )
        log_output = log_buffer.getvalue()

    return await to_agent_response(context, response, log_output)


def _preprocess_line(context: Context, line: str) -> str:
    if not line.strip().startswith("//"):
        return line

    line = line.strip()
    try:
        words = shlex.split(line[2:].strip())
    except ValueError:
        words = line[2:].strip().split()

    if not words:
        return line

    name = words[0]
    if name.endswith(":"):
        name = name[:-1]

    path = get_prompt_path(
        name, context.team.project.get_language_code(), context.person.person_id
    )
    if not path.exists():
        return line

    prompt = load_markdown_with_frontmatter(path)
    return get_body_from_prompt(prompt, words[1:])


def preprocess(context: Context, text: str) -> str:
    lines = text.strip().splitlines()
    for i, line in enumerate(lines):
        lines[i] = _preprocess_line(context, line)
    return "\n".join(lines)
