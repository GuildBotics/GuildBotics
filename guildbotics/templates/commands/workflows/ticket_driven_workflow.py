import os
import re
from pathlib import Path
from typing import Any

from guildbotics.capabilities.completion_retry import (
    CLI_AGENT_CONVERSATION_FILE_ENV,
    run_with_completion_retry,
)
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
    get_workspace_data_root,
)
from guildbotics.utils.i18n_tool import t

TICKET_MAX_ATTEMPTS_ENV = "GUILDBOTICS_TICKET_MAX_ATTEMPTS"
_DEFAULT_MAX_ATTEMPTS = 5


def _max_agent_attempts() -> int:
    """Number of agent turns per ticket dispatch before giving up.

    A turn that leaves no terminal completion record is retried (resuming the
    previous conversation) so a slow, multi-turn CLI agent can finish; the budget
    bounds that so a permanently failing turn cannot loop.
    """
    raw = os.getenv(TICKET_MAX_ATTEMPTS_ENV, "").strip()
    try:
        return max(1, int(raw)) if raw else _DEFAULT_MAX_ATTEMPTS
    except ValueError:
        return _DEFAULT_MAX_ATTEMPTS


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


def _task_run_status(
    run_id: str, task_run_root: Path, member_workspace: Path
) -> TaskRunStatus:
    first_error: TaskRunError | None = None
    stores = [
        TaskRunStore(task_run_root),
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
    workspace_data_root = get_workspace_data_root()
    member_workspace = workspace_data_root / "workspaces" / context.person.person_id
    member_workspace.mkdir(parents=True, exist_ok=True)

    last_response: list[Any] = []

    async def _invoke_ticket_turn(run_id: str, _attempt: int) -> None:
        # Scope the run id to this agent subprocess only. Mutating the
        # process-global os.environ would race across the scheduler's
        # per-member worker threads; the brain merges this overlay into the
        # subprocess env so child ``guildbotics member`` calls record evidence
        # under the correct run id. The conversation file pins this dispatch's
        # agent conversation so retries resume it by id (not whatever last ran in
        # this cwd). Ensure its parent exists so the agent can write it even when
        # the first attempt records no evidence (task-runs not created yet).
        conversation_file = (
            workspace_data_root / "task-runs" / f"{run_id}.agy-conversation"
        )
        conversation_file.parent.mkdir(parents=True, exist_ok=True)
        cli_agent_env = {
            TASK_RUN_ENV: run_id,
            GUILDBOTICS_DATA_DIR: str(workspace_data_root),
            CLI_AGENT_CONVERSATION_FILE_ENV: str(conversation_file),
        }
        response = await context.invoke(
            "functions/handle_github_ticket",
            person_id=context.person.person_id,
            ticket_url=ticket_url,
            pull_request_url=context.task.pull_request_url or "",
            work_type=_work_type(context.task),
            trigger_reason=context.task.trigger_reason or "",
            language=context.language_name,
            member_workspace=str(member_workspace),
            workflow_run_id=run_id,
            prepare_command=_prepare_command(context, ticket_url),
            cli_agent_env=cli_agent_env,
            cwd=member_workspace,
        )
        last_response.append(response)

    # Retry the agent in-process until it records a terminal completion. On
    # exhaustion this raises CompletionRetryExhausted, which main() turns into a
    # ticket comment; that comment then stops the ticket from being re-selected.
    completion, _run_id = await run_with_completion_retry(
        invoke=_invoke_ticket_turn,
        check_completion=lambda rid: _task_run_status(
            rid, workspace_data_root / "task-runs", member_workspace
        ),
        max_attempts=_max_agent_attempts(),
    )
    agent_response = _normalize_agent_response(
        last_response[-1] if last_response else None
    )
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


async def main(context: Context) -> AgentResponse | None:
    """
    Poll the ticket manager for one actionable GitHub issue or PR and delegate the
    actual GitHub/git/PR work to the configured CLI agent.
    """
    ticket_manager = context.get_ticket_manager()

    task = None
    shared_state = getattr(context, "shared_state", None)
    if isinstance(shared_state, dict):
        from guildbotics.runtime.workflow_invocation import (
            WORKFLOW_INVOCATION_KEY,
            WorkflowInvocation,
        )

        invocation = shared_state.get(WORKFLOW_INVOCATION_KEY)
        if invocation is not None:
            payload = None
            if (
                isinstance(invocation, dict)
                and invocation.get("trigger_type") == "ticket"
            ):
                payload = invocation.get("payload")
            elif (
                isinstance(invocation, WorkflowInvocation)
                and invocation.trigger_type == "ticket"
            ):
                payload = invocation.payload

            if payload and "task" in payload:
                task = Task(**payload["task"])

    if task is None:
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
