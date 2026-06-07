import datetime
import traceback
from pathlib import Path
from typing import Any

from guildbotics.entities.task import Task
from guildbotics.integrations.code_hosting_service import CodeHostingService
from guildbotics.integrations.ticket_manager import TicketManager
from guildbotics.intelligences.common import (
    AgentResponse,
    GitHubTicketAgentResult,
    Labels,
)
from guildbotics.runtime import Context
from guildbotics.templates.commands.workflows.modes.util import (
    get_branch_name,
    get_git_tool,
)
from guildbotics.utils.fileio import get_storage_path
from guildbotics.utils.git_tool import GitTool
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.log_utils import get_log_output_dir


async def _move_task_to_working_if_ready(
    context: Context, ticket_manager: TicketManager
) -> None:
    """Move a newly selected task to the working lane when one is available."""
    if context.task.status == Task.READY and context.task.id is not None:
        await ticket_manager.move_ticket(context.task, Task.IN_PROGRESS)
        context.task.status = Task.IN_PROGRESS


def _safe_display_path(path: Path) -> str:
    expanded_home = Path.home().expanduser()
    resolved_path = path.expanduser()
    try:
        relative_path = resolved_path.relative_to(expanded_home)
        return str(Path("~") / relative_path)
    except ValueError:
        return str(resolved_path)


def _error_log_dir() -> Path:
    return get_log_output_dir("ticket_driven_workflow") or (
        get_storage_path() / "logs" / "ticket_driven_workflow"
    )


def _write_task_error_log(context: Context, error: Exception) -> Path:
    log_dir = _error_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
    log_file_path = log_dir / f"ticket_workflow_error_{timestamp}.log"
    error_text = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    log_file_path.write_text(error_text, encoding="utf-8")
    context.logger.error("Ticket workflow error log: %s", log_file_path)
    return log_file_path


async def _build_task_error_message(
    context: Context, error: Exception | None = None
) -> str:
    error_text = t("drivers.task_scheduler.task_error")
    if error is not None:
        log_file_path = _write_task_error_log(context, error)
        error_text = (
            f"{error_text}\n\n"
            f"{t('drivers.task_scheduler.task_error_log_path', path=_safe_display_path(log_file_path))}"
        )
    try:
        from guildbotics.intelligences.functions import talk_as

        talked_text = await talk_as(context, error_text, "Ticket", [])
        return talked_text or error_text
    except Exception:
        return error_text


def _normalize_agent_result(response: Any) -> GitHubTicketAgentResult:
    if isinstance(response, GitHubTicketAgentResult):
        return response
    if isinstance(response, AgentResponse):
        return GitHubTicketAgentResult(
            status=response.status,
            summary=response.message,
            question=response.message
            if response.status == AgentResponse.ASKING
            else "",
            ticket_comment=response.message,
        )
    return GitHubTicketAgentResult(
        status=GitHubTicketAgentResult.DONE,
        summary=str(response) if response is not None else "",
    )


def _work_type(task: Task) -> str:
    if task.pull_request_url:
        return "pull_request_review"
    return "issue"


def _format_issue_comments(task: Task) -> str:
    if not task.comments:
        return "(none)"
    return "\n\n".join(
        f"[{comment.author_type}] {comment.author}: {comment.content}"
        for comment in task.comments
    )


def _format_review_context(review_comments: object) -> str:
    inline_threads = getattr(review_comments, "inline_comment_threads", [])
    body = str(review_comments).strip()
    thread_text = "\n\n".join(
        f"{thread!s}\n**Reply target comment_id:** {_thread_reply_target_comment_id(thread)}"
        for thread in inline_threads
    )
    return "\n\n".join(part for part in [body, thread_text] if part) or "(none)"


async def _checkout_work_branch(
    context: Context,
    git_tool: GitTool,
    code_hosting_service: CodeHostingService,
) -> None:
    if context.task.pull_request_url:
        branch = await code_hosting_service.get_pull_request_head_branch(
            context.task.pull_request_url
        )
    else:
        branch = get_branch_name(context)
    git_tool.checkout_branch(branch)


async def _load_review_context(
    context: Context, code_hosting_service: CodeHostingService
) -> tuple[Any | None, str]:
    if not context.task.pull_request_url:
        return None, ""
    review_comments = await code_hosting_service.get_pull_request_comments(
        context.task.pull_request_url
    )
    return review_comments, _format_review_context(review_comments)


def _done_message(result: GitHubTicketAgentResult, fallback: str = "") -> str:
    return result.ticket_comment or result.summary or fallback


async def _post_agent_question(
    result: GitHubTicketAgentResult,
    ticket_manager: TicketManager,
    task: Task,
) -> AgentResponse:
    message = result.ticket_comment or result.question or result.summary
    if message:
        await ticket_manager.add_comment_to_ticket(task, message)
    return AgentResponse(
        status=AgentResponse.ASKING,
        message=message,
        skip_ticket_comment=True,
    )


def _build_ticket_draft_tasks(
    result: GitHubTicketAgentResult, source_task: Task
) -> list[Task]:
    tasks = []
    for item in result.new_tickets:
        task = item.to_task()
        task.status = Task.READY
        task.repository = source_task.repository
        task.owner = source_task.owner
        tasks.append(task)
    return tasks


async def _create_ticket_drafts(
    result: GitHubTicketAgentResult,
    ticket_manager: TicketManager,
    source_task: Task,
) -> list[str]:
    tasks = _build_ticket_draft_tasks(result, source_task)
    if not tasks:
        return []
    await ticket_manager.create_tickets(tasks)
    return [await ticket_manager.get_ticket_url(task) for task in tasks]


def _format_created_ticket_message(ticket_urls: list[str]) -> str:
    if not ticket_urls:
        return ""
    return t(
        "commands.workflows.ticket_driven_workflow.ticket_drafts_created",
        task_labels=Labels(ticket_urls),
    )


def _append_created_ticket_message(message: str, ticket_urls: list[str]) -> str:
    created_ticket_message = _format_created_ticket_message(ticket_urls)
    if not created_ticket_message:
        return message
    if not message:
        return created_ticket_message
    return f"{message}\n\n{created_ticket_message}"


def _thread_reply_target_comment_id(thread: Any) -> int | None:
    comments = getattr(thread, "comments", [])
    if not comments:
        return None
    return getattr(comments[-1], "comment_id", None)


def _apply_pull_request_review_replies(
    context: Context,
    review_comments: Any,
    result: GitHubTicketAgentResult,
    fallback_reply: str,
) -> None:
    inline_threads = getattr(review_comments, "inline_comment_threads", [])
    reply_by_comment_id = {
        reply.comment_id: reply.reply.strip()
        for reply in result.review_replies
        if reply.reply.strip()
    }
    for thread in inline_threads:
        comment_id = _thread_reply_target_comment_id(thread)
        if comment_id is None:
            continue
        reply = reply_by_comment_id.get(comment_id)
        if not reply:
            continue
        if hasattr(thread, "add_reply"):
            thread.add_reply(reply)
        else:
            thread.reply = reply

    if result.review_reply:
        if inline_threads:
            context.logger.warning(
                "CLI agent returned general review_reply while inline review "
                "threads are present; posting it as a PR conversation comment."
            )
        review_comments.reply = fallback_reply
    elif not inline_threads and fallback_reply:
        review_comments.reply = fallback_reply


async def _publish_result(
    context: Context,
    ticket_manager: TicketManager,
    code_hosting_service: CodeHostingService,
    git_tool: GitTool,
    result: GitHubTicketAgentResult,
    review_comments: Any | None,
) -> AgentResponse:
    if result.status == GitHubTicketAgentResult.ASKING:
        return await _post_agent_question(result, ticket_manager, context.task)

    created_ticket_urls = await _create_ticket_drafts(
        result, ticket_manager, context.task
    )

    diff = git_tool.get_diff()
    commit_sha = None
    if diff:
        commit_sha = git_tool.commit_changes(
            result.commit_message or context.task.title or "Update from ticket"
        )

    if context.task.pull_request_url:
        message = _append_created_ticket_message(
            result.review_reply or result.ticket_comment or result.summary,
            created_ticket_urls,
        )
        if review_comments is not None:
            reply = f"{message}\n{commit_sha}" if commit_sha and message else message
            _apply_pull_request_review_replies(context, review_comments, result, reply)
            await code_hosting_service.respond_to_comments(
                context.task.pull_request_url, review_comments
            )
        return AgentResponse(
            status=AgentResponse.DONE,
            message=message or context.task.pull_request_url,
            skip_ticket_comment=True,
        )

    if commit_sha:
        ticket_url = await ticket_manager.get_ticket_url(context.task, markdown=False)
        pull_request_url = await code_hosting_service.create_pull_request(
            branch_name=get_branch_name(context),
            title=result.pr_title or context.task.title,
            description=result.pr_body or result.summary,
            ticket_url=ticket_url,
        )
        message = _done_message(
            result,
            fallback=f"Completed the work. Please review {pull_request_url}",
        )
        message = _append_created_ticket_message(message, created_ticket_urls)
        ticket_comment = f"{message}\n\n{Task.OUTPUT_PREFIX}[{context.task.title}]({pull_request_url})"
        await ticket_manager.add_comment_to_ticket(context.task, ticket_comment)
        return AgentResponse(
            status=AgentResponse.DONE,
            message=ticket_comment,
            skip_ticket_comment=True,
        )

    message = _append_created_ticket_message(_done_message(result), created_ticket_urls)
    if message:
        await ticket_manager.add_comment_to_ticket(context.task, message)
    return AgentResponse(
        status=AgentResponse.DONE,
        message=message,
        skip_ticket_comment=True,
    )


async def _main(context: Context, ticket_manager: TicketManager) -> AgentResponse:
    await _move_task_to_working_if_ready(context, ticket_manager)

    git_tool = await get_git_tool(context)
    code_hosting_service = context.get_code_hosting_service(context.task.repository)
    await _checkout_work_branch(context, git_tool, code_hosting_service)
    review_comments, review_context = await _load_review_context(
        context, code_hosting_service
    )
    ticket_url = await ticket_manager.get_ticket_url(context.task, markdown=False)
    initial_head = git_tool.repo.head.commit.hexsha
    response = await context.invoke(
        "functions/handle_github_ticket",
        ticket_url=ticket_url,
        pull_request_url=context.task.pull_request_url or "",
        work_type=_work_type(context.task),
        trigger_reason=context.task.trigger_reason or "",
        issue_title=context.task.title,
        issue_description=context.task.description or "",
        issue_comments=_format_issue_comments(context.task),
        review_context=review_context,
        language=context.language_name,
        cwd=git_tool.repo_path,
    )
    if git_tool.repo.head.commit.hexsha != initial_head:
        raise RuntimeError(
            "CLI agent created a git commit directly. "
            "GitHub and git write operations must be performed by GuildBotics."
        )
    result = _normalize_agent_result(response)
    return await _publish_result(
        context,
        ticket_manager,
        code_hosting_service,
        git_tool,
        result,
        review_comments,
    )


async def main(context: Context) -> AgentResponse | None:
    """
    Poll the ticket manager for one actionable GitHub issue or PR and delegate the
    actual GitHub/git/PR work to the configured CLI agent.
    """
    ticket_manager = context.get_ticket_manager()
    task = await ticket_manager.get_task_to_work_on()
    if task is None:
        return None

    context.update_task(task)
    try:
        response = await _main(context, ticket_manager)
        if (
            response.status == AgentResponse.ASKING
            and not response.skip_ticket_comment
            and response.message
        ):
            await ticket_manager.add_comment_to_ticket(task, response.message)
            response.skip_ticket_comment = True
        return response
    except Exception as error:
        message = await _build_task_error_message(context, error)
        await ticket_manager.add_comment_to_ticket(task, message)
        raise
