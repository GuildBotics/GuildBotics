import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from guildbotics.capabilities.task_runs import (
    TASK_RUN_ENV,
    TaskRunError,
    TaskRunStatus,
    TaskRunStore,
)
from guildbotics.entities.task import Task
from guildbotics.integrations.ticket_manager import TicketManager
from guildbotics.intelligences.common import AgentResponse
from guildbotics.observability import set_attributes
from guildbotics.runtime import Context
from guildbotics.utils.fileio import (
    GUILDBOTICS_DATA_DIR,
    get_storage_path,
    get_workspace_path,
)
from guildbotics.utils.i18n_tool import t


async def _move_task_to_working_if_ready(
    context: Context, ticket_manager: TicketManager
) -> None:
    """Move a newly selected task to the working lane when one is available."""
    if context.task.status == Task.READY and context.task.id is not None:
        moved = await ticket_manager.move_ticket(context.task, Task.IN_PROGRESS)
        if moved:
            context.task.status = Task.IN_PROGRESS


def _ticket_trace_attributes(task: Task) -> dict[str, str]:
    """Correlation attributes that make a ticket run findable in diagnostics."""
    attributes: dict[str, str] = {}
    if task.repository:
        attributes["github.repo"] = task.repository
    if task.pull_request_url:
        attributes["github.kind"] = "pull_request"
        attributes["github.url"] = task.pull_request_url
        match = re.search(r"/pull/(\d+)", task.pull_request_url)
        if match:
            attributes["github.number"] = match.group(1)
    else:
        attributes["github.kind"] = "issue"
        if task.url:
            attributes["github.url"] = task.url
        if task.number is not None:
            attributes["github.number"] = str(task.number)
    return attributes


async def _build_task_error_message(
    context: Context, error: Exception | None = None
) -> str:
    # The traceback is logged (trace-scoped ERROR) by the command runner on
    # re-raise; the ticket comment stays a safe, reader-facing message with no
    # local paths or internal details.
    error_text = t("drivers.task_scheduler.task_error")
    try:
        from guildbotics.intelligences.functions import talk_as

        talked_text = await talk_as(context, error_text, "Ticket", [])
        return talked_text or error_text
    except Exception:
        return error_text


def _work_type(task: Task) -> str:
    if task.pull_request_url:
        return "pull_request_review"
    return "issue"


def _normalize_agent_response(response: Any) -> AgentResponse:
    if isinstance(response, AgentResponse):
        return response
    return AgentResponse(
        status=AgentResponse.DONE,
        message=str(response) if response is not None else "",
        skip_ticket_comment=True,
    )


def _task_run_status(run_id: str, member_workspace: Path) -> TaskRunStatus:
    first_error: TaskRunError | None = None
    stores = [
        TaskRunStore(),
        TaskRunStore(member_workspace / ".guildbotics-data" / "task-runs"),
        TaskRunStore(member_workspace / ".guildbotics" / "data" / "task-runs"),
    ]
    for store in stores:
        try:
            return store.status(run_id)
        except TaskRunError as exc:
            first_error = first_error or exc
    if first_error is not None:
        raise first_error
    raise TaskRunError(f"Task run '{run_id}' was not found.")


async def _main(context: Context, ticket_manager: TicketManager) -> AgentResponse:
    await _move_task_to_working_if_ready(context, ticket_manager)

    ticket_url = await ticket_manager.get_ticket_url(context.task, markdown=False)
    workflow_run_id = uuid4().hex
    member_workspace = get_workspace_path(context.person.person_id)
    member_workspace.mkdir(parents=True, exist_ok=True)
    response = await context.invoke(
        "functions/handle_github_ticket",
        person_id=context.person.person_id,
        ticket_url=ticket_url,
        pull_request_url=context.task.pull_request_url or "",
        work_type=_work_type(context.task),
        trigger_reason=context.task.trigger_reason or "",
        language=context.language_name,
        member_workspace=str(member_workspace),
        workflow_run_id=workflow_run_id,
        prepare_command=_prepare_command(context, ticket_url),
        github_capability_help=_github_capability_help(),
        # Scope the run id to this agent subprocess only. Mutating the
        # process-global os.environ would race across the scheduler's
        # per-member worker threads; the brain merges this overlay into the
        # subprocess env so child ``guildbotics member`` calls record evidence
        # under the correct run id.
        cli_agent_env={
            TASK_RUN_ENV: workflow_run_id,
            GUILDBOTICS_DATA_DIR: str(get_storage_path()),
        },
        cwd=member_workspace,
    )
    agent_response = _normalize_agent_response(response)
    completion = _task_run_status(workflow_run_id, member_workspace)
    return AgentResponse(
        status=(
            AgentResponse.ASKING
            if completion.status == AgentResponse.ASKING
            else AgentResponse.DONE
        ),
        message=agent_response.message or completion.summary,
        skip_ticket_comment=True,
    )


def _prepare_command(context: Context, ticket_url: str) -> str:
    """Build the exact ``git prepare`` command for this run.

    The workflow already knows whether this is a PR review (``pull_request_url``
    is set), so it hands the agent a ready-to-run command. For PR review this
    includes ``--pr-url`` so the PR head branch is checked out; without it
    ``prepare`` would fall back to issue mode and silently work on a new
    ``ticket/<n>`` branch instead of the PR under review.
    """
    person_id = context.person.person_id
    command = (
        f"guildbotics member git prepare --person {person_id} --issue-url {ticket_url}"
    )
    if context.task.pull_request_url:
        command += f" --pr-url {context.task.pull_request_url}"
    return command


def _github_capability_help() -> str:
    return "\n".join(
        [
            "guildbotics member context --person <person> --check-credentials",
            "guildbotics member github issue inspect --person <person> --url <issue_url>",
            "guildbotics member github pr inspect --person <person> --url <pr_url> --include-comments",
            "guildbotics member git prepare --person <person> --issue-url <issue_url> [--pr-url <pr_url>]",
            "guildbotics member git publish --person <person> --repo-path <path> --message-file <file>",
            "guildbotics member github pr create --person <person> --repo <owner/repo> --head <branch> --title-file <file> --body-file <file> --issue-url <issue_url>",
            "guildbotics member github issue comment --person <person> --url <issue_url> --body-file <file>",
            "guildbotics member github pr reply --person <person> --url <pr_url> --reply-target-id <id> --body-file <file>",
            "guildbotics member github reaction add --person <person> --repo <owner/repo> --target issue-comment|pr-review-comment --comment-id <id> --reaction +1",
            "guildbotics member task complete --person <person> --run-id <run_id> --ticket-url <issue_url> --status done|asking|blocked --summary-file <file>",
        ]
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
    set_attributes(**_ticket_trace_attributes(task))
    try:
        return await _main(context, ticket_manager)
    except Exception as error:
        message = await _build_task_error_message(context, error)
        await ticket_manager.add_comment_to_ticket(task, message)
        raise
