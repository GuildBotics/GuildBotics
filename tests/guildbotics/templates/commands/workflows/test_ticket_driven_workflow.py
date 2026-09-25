"""The ticket workflow runs one AI CLI turn with the input the host selected.

Selection, the working-lane move, the run id and completion budget, and the
status comments of a failed or rate-limited run belong to the host's
``TicketSelector`` (``tests/guildbotics/drivers/test_ticket_selector.py``).
"""

from pathlib import Path

import pytest

from guildbotics.drivers.agent_turn import AgentTurnResult
from guildbotics.intelligences.common import AgentResponse
from guildbotics.runtime.workflow_invocation import (
    WORKFLOW_INVOCATION_KEY,
    WorkflowInvocation,
)
from guildbotics.templates.commands.workflows import ticket_driven_workflow
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.i18n_tool import get_language, set_language

ISSUE_URL = "https://github.com/GuildBotics/GuildBotics/issues/1"
PR_URL = "https://github.com/GuildBotics/GuildBotics/pull/2"


@pytest.fixture(autouse=True)
def _isolated_workspace_data(monkeypatch, tmp_path):
    previous_language = get_language()
    set_language("en")
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    yield
    set_language(previous_language)


class _Person:
    person_id = "aiko"


class _Context:
    """Holds the host's invocation; it has no ticket manager to reach for."""

    def __init__(self, pull_request_url: str = "", trigger_reason: str = "") -> None:
        self.person = _Person()
        self.language_name = "English"
        self.invocations: list[tuple[str, dict]] = []
        self.shared_state = {
            WORKFLOW_INVOCATION_KEY: WorkflowInvocation(
                command="workflows/ticket_driven_workflow",
                person_id="aiko",
                source="routine",
                trigger_type="ticket",
                payload={
                    "task": {"title": "T", "description": "D"},
                    "ticket_url": pull_request_url or ISSUE_URL,
                    "pull_request_url": pull_request_url,
                    "trigger_reason": trigger_reason,
                    "run_id": "trace-7",
                    "max_completion_attempts": 3,
                },
            )
        }
        self.response = AgentResponse(status=AgentResponse.DONE, message="done")

    async def invoke(self, command_name: str, **kwargs):
        self.invocations.append((command_name, kwargs))
        return AgentTurnResult(response=self.response, completion=None, evidence=[])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_workflow_runs_one_turn_with_the_host_selected_ticket(tmp_path):
    context = _Context()

    response = await ticket_driven_workflow.main(context)  # type: ignore[arg-type]

    assert response is context.response
    [(command_name, kwargs)] = context.invocations
    assert command_name == "functions/handle_github_ticket"
    assert kwargs["agent_execution_context"] == {
        "run_id": "trace-7",
        "workspace_data_root": str(Path(tmp_path)),
        "work_kind": "ticket",
        "work_identity": ISSUE_URL,
        "resume_policy": "fresh",
        "attempt": 1,
        "max_completion_attempts": 3,
    }
    assert kwargs["workflow_run_id"] == "trace-7"
    assert kwargs["person_id"] == "aiko"
    assert kwargs["ticket_url"] == ISSUE_URL
    assert kwargs["pull_request_url"] == ""
    assert kwargs["work_type"] == "issue"
    assert kwargs["trigger_reason"] == ""
    # The workflow does not read or pass issue content; the agent inspects it.
    assert "issue_title" not in kwargs
    assert "issue_description" not in kwargs
    assert kwargs["language"] == "English"
    assert kwargs["member_workspace"] == str(
        Path(tmp_path) / ".guildbotics" / "local" / "clones" / "aiko"
    )
    assert kwargs["cwd"] == Path(kwargs["member_workspace"])
    # Issue trigger: prepare command has no --pr-url.
    assert kwargs["prepare_command"] == (
        f"guildbotics member git prepare --person aiko --issue-url {ISSUE_URL}"
    )
    # The capability reference is not injected per-prompt; the agent reads it
    # from the mandatory `member context` call (the single source of truth).
    assert "github_capability_help" not in kwargs
    # The shared workflow envelope is injected from the single i18n source.
    assert "guildbotics_execution_mode=workflow" in kwargs["workflow_contract"]
    assert "guildbotics member context --person aiko" in kwargs["workflow_contract"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger_reason", "work_type"),
    [
        ("pull_request_feedback", "pull_request_feedback"),
        ("pull_request_review", "pull_request_review"),
        ("", "pull_request_feedback"),
    ],
)
async def test_workflow_passes_pull_request_work_type(trigger_reason, work_type):
    context = _Context(PR_URL, trigger_reason)

    await ticket_driven_workflow.main(context)  # type: ignore[arg-type]

    kwargs = context.invocations[0][1]
    assert kwargs["pull_request_url"] == PR_URL
    assert kwargs["ticket_url"] == PR_URL
    # The patrol names the member's role on the PR; the prompt branches on it.
    assert kwargs["work_type"] == work_type
    # Pull request work anchors on --pr-url so the agent checks out the PR head
    # branch instead of a fresh ticket/<n> branch.
    assert kwargs["prepare_command"].endswith(f"--pr-url {PR_URL}")
    assert "--issue-url" not in kwargs["prepare_command"]
