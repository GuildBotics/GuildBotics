from typing import Any

from guildbotics.runtime import Context
from guildbotics.runtime.workflow_invocation import WORKFLOW_INVOCATION_KEY
from guildbotics.utils.fileio import get_member_clone_path, get_workspace_root
from guildbotics.utils.i18n_tool import t

COMMAND_METADATA = {
    "name": {
        "en": "Ticket-driven workflow",
        "ja": "チケット駆動ワークフロー",
    },
    "description": {
        "en": (
            "Poll the ticket manager for one actionable GitHub issue or PR and "
            "delegate it to the AI CLI tool."
        ),
        "ja": (
            "対応可能な GitHub issue または PR を1件取得し、AI CLIツールへ委譲します。"
        ),
    },
    "routine": True,
}


def _prepare_command(person_id: str, ticket_url: str, pull_request_url: str) -> str:
    """Build the exact ``git prepare`` command for this run.

    The workflow already knows whether this is pull request work
    (``pull_request_url`` is set), so it hands the agent a ready-to-run
    command. Pull request work anchors on ``--pr-url`` so the PR head branch
    is checked out; without it ``prepare`` would work on a new ``ticket/<n>``
    branch instead of the PR.
    """
    if pull_request_url:
        return (
            f"guildbotics member git prepare --person {person_id} "
            f"--pr-url {pull_request_url}"
        )
    return (
        f"guildbotics member git prepare --person {person_id} --issue-url {ticket_url}"
    )


async def main(context: Context) -> Any:
    """Work on the GitHub issue or PR the host selected, with an AI CLI turn.

    The host has already selected the ticket, moved it to the working lane,
    and settles how the run ends; this workflow asks for the turn, which the
    host drives until the member records a completion, and raises when it
    cannot.
    """
    turn = context.shared_state[WORKFLOW_INVOCATION_KEY].payload
    person_id = context.person.person_id
    ticket_url = turn["ticket_url"]
    pull_request_url = turn["pull_request_url"]
    trigger_reason = turn["trigger_reason"]
    member_workspace = get_member_clone_path(person_id)
    member_workspace.mkdir(parents=True, exist_ok=True)
    return await context.invoke(
        "functions/handle_github_ticket",
        person_id=person_id,
        workflow_contract=t(
            "commands.workflows.common.workflow_contract",
            person_id=person_id,
        ),
        ticket_url=ticket_url,
        pull_request_url=pull_request_url,
        # ``issue``, or the pull request role the host selected the ticket for.
        work_type=(
            (trigger_reason or "pull_request_feedback") if pull_request_url else "issue"
        ),
        trigger_reason=trigger_reason,
        language=context.language_name,
        member_workspace=str(member_workspace),
        workflow_run_id=turn["run_id"],
        prepare_command=_prepare_command(person_id, ticket_url, pull_request_url),
        agent_execution_context={
            "run_id": turn["run_id"],
            "workspace_data_root": str(get_workspace_root()),
            "work_kind": "ticket",
            "work_identity": ticket_url,
            "resume_policy": "fresh",
            "attempt": 1,
            "max_completion_attempts": turn["max_completion_attempts"],
        },
        cwd=member_workspace,
    )
