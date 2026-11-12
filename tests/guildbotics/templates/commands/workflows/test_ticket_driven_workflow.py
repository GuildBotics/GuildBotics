"""The ticket workflow runs one AI CLI turn with the input the host selected.

Selection, the working-lane move, the run id and completion budget, and the
status comments of a failed or rate-limited run belong to the host's
``TicketSelector`` (``tests/guildbotics/drivers/test_ticket_selector.py``).
"""

import asyncio
import importlib
import json
from pathlib import Path

import os

import pytest

from guildbotics.entities.task import Task
from guildbotics.intelligences.common import AgentResponse
from guildbotics.runtime.member_invocation import MemberInvocation, Work
from guildbotics.runtime.person_lease import PersonExecutionLease
from guildbotics.runtime.workflow_invocation import (
    WORKFLOW_INVOCATION_KEY,
    WorkflowInvocation,
)
from guildbotics.templates.commands.workflows import ticket_driven_workflow
from guildbotics.utils.correlation import trace_scope

ISSUE_URL = "https://github.com/GuildBotics/GuildBotics/issues/1"
PR_URL = "https://github.com/GuildBotics/GuildBotics/pull/2"


class _Person:
    person_id = "aiko"


class _Context:
    """Holds the host's invocation; it has no ticket service to reach for."""

    def __init__(
        self,
        pull_request_url: str = "",
        trigger_reason: str = "",
        resumed: dict[str, str] | None = None,
        work: Work | None = None,
    ) -> None:
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
                    "awaited_run_id": "",
                    "awaited_summary": "",
                    **(resumed or {}),
                    "max_completion_attempts": 3,
                },
                work=work or Work.of_ticket(pull_request_url or ISSUE_URL),
            )
        }
        self.response = AgentResponse(status=AgentResponse.DONE, message="done")

    async def invoke(self, command_name: str, **kwargs):
        self.invocations.append((command_name, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_workflow_runs_one_turn_with_the_host_selected_ticket(tmp_path):
    context = _Context()

    response = await ticket_driven_workflow.main(context)  # type: ignore[arg-type]

    assert response is context.response
    [(command_name, kwargs)] = context.invocations
    assert kwargs["artifact_directory"] == os.getcwd()
    assert command_name == "functions/handle_ticket"
    # The run and the work are the host's: the turn takes them from its grant.
    assert kwargs["agent_execution_context"] == {
        "resume_policy": "auto",
        "attempt": 1,
        "max_completion_attempts": 3,
    }
    assert kwargs["person_id"] == "aiko"
    assert kwargs["ticket_url"] == ISSUE_URL
    assert kwargs["pull_request_url"] == ""
    assert kwargs["work_type"] == "ticket"
    assert kwargs["trigger_reason"] == ""
    # The workflow does not read or pass ticket content; the agent inspects it.
    assert "issue_title" not in kwargs
    assert "issue_description" not in kwargs
    assert kwargs["language"] == "English"
    assert kwargs["member_workspace"] == str(
        Path(tmp_path) / ".guildbotics" / "local" / "clones" / "aiko"
    )
    assert kwargs["cwd"] == Path(kwargs["member_workspace"])
    # Ticket trigger: prepare command has no --pr-url.
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
async def test_a_resumed_run_continues_the_awaited_work_and_its_session():
    """A run the patrol resumed for its PR's checks runs in the work the
    awaiting run did, continues that conversation, and is told what that run
    said of where the work came from."""
    resumed = {
        "awaited_run_id": "run-1",
        "awaited_summary": "Opened the PR for the thread; report there.",
    }
    work = Work("chat", "slack:U1:C1:T1")
    context = _Context(PR_URL, "pull_request_checks", resumed, work)

    await ticket_driven_workflow.main(context)  # type: ignore[arg-type]

    kwargs = context.invocations[0][1]
    assert kwargs["work_type"] == "pull_request_checks"
    assert kwargs["agent_execution_context"]["resume_policy"] == "auto"
    assert {
        key: kwargs[key]
        for key in (
            *resumed,
            "work_kind",
            "work_identity",
            "chat_channel_id",
            "chat_thread_id",
        )
    } == {
        **resumed,
        "work_kind": "chat",
        "work_identity": "slack:U1:C1:T1",
        "chat_channel_id": "C1",
        "chat_thread_id": "T1",
    }


@pytest.mark.asyncio
async def test_a_new_run_continues_its_works_session_without_a_checks_wait():
    """A reply after a timeout reuses the session without a checks wait."""
    context = _Context(PR_URL, "pull_request_feedback")

    await ticket_driven_workflow.main(context)  # type: ignore[arg-type]

    kwargs = context.invocations[0][1]
    assert kwargs["agent_execution_context"]["resume_policy"] == "auto"
    assert (kwargs["awaited_run_id"], kwargs["work_kind"]) == ("", "ticket")


@pytest.mark.asyncio
async def test_mention_work_passes_the_response_only_work_type():
    context = _Context(
        trigger_reason="issue_mention", work=Work("ticket_mention", ISSUE_URL)
    )
    await ticket_driven_workflow.main(context)
    kwargs = context.invocations[0][1]
    assert kwargs["work_type"] == "ticket_mention"
    assert kwargs["work_kind"] == "ticket_mention"
    assert kwargs["pull_request_url"] == ""


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
    # branch instead of a fresh ticket/<key> branch.
    assert kwargs["prepare_command"].endswith(f"--pr-url {PR_URL}")
    assert "--issue-url" not in kwargs["prepare_command"]
