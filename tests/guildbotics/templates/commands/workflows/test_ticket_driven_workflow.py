"""The ticket workflow runs one AI CLI turn with the input the host selected.

Selection, the working-lane move, the run id and completion budget, and the
status comments of a failed or rate-limited run belong to the host's
``TicketSelector`` (``tests/guildbotics/drivers/test_ticket_selector.py``).
"""

import asyncio
import importlib
import json
from pathlib import Path

import pytest

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.drivers.ticket_selector import TicketSelector
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
from tests.guildbotics.local_code_host import (
    REPOSITORY,
    comment,
    item,
    local_member,
    pull_request,
    remote_with,
    ticket,
)

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


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_pr", [False, True])
@pytest.mark.parametrize("mention_only", [False, True])
async def test_a_ticket_is_selected_moved_worked_and_completed_on_the_local_board(
    monkeypatch, tmp_path, linked_pr, mention_only
):
    """One round of the ticket workflow on the local code host and board: the
    host selects the member's ready ticket and moves it to the working lane,
    the turn answers on it with the member's commands, and its completion is
    accepted against the ticket. With an open PR, the human's answer resumes
    ticket work and the turn reuses that PR before answering on the ticket."""
    member_cli = importlib.import_module("guildbotics.cli.member")
    member = local_member()
    url = ticket(
        1,
        title="Fix login",
        lane=Task.IN_PROGRESS if linked_pr else Task.READY,
        assignees=["yuki" if mention_only else "aiko"],
        comments=[comment("owner", "@aiko Please provide context.")]
        if mention_only
        else [
            comment("aiko", "Should I add the check?"),
            comment("owner", "Yes, please add the check."),
        ]
        if linked_pr
        else [],
    )
    if linked_pr:
        remote_with("ticket/T-1")
        pr_url = pull_request(
            1,
            "ticket/T-1",
            author="aiko",
            tickets=[{"url": url, "closes": False}],
        )
    monkeypatch.setattr(
        member_cli, "resolve_member_context", lambda _: (member, member.person)
    )
    monkeypatch.setattr(member_cli, "prepare_commit_and_push_once", lambda: None)
    lanes: list[str] = []

    class _Selection:
        """The member's context as the host's selection clones it."""

        person = member.person
        team = member.team

        def clone_for(self, person):
            return self

        def get_ticket_service(self):
            return member.tickets

        def get_code_hosting_service(self):
            return member.code

        async def aclose(self):
            pass

    class _Turn(_Context):
        """The workflow's context; its one turn is the member's commands."""

        async def invoke(self, command_name: str, **kwargs):
            lanes.append(item(url)["lane"])
            assert kwargs["work_type"] == (
                "ticket_mention" if mention_only else "ticket"
            )
            assert kwargs["trigger_reason"] == (
                "issue_mention"
                if mention_only
                else "issue_comment"
                if linked_pr
                else "ready_lane"
            )
            invocation = MemberInvocation(
                task_run_id=run.run_id,
                work=run.work,
                lease=PersonExecutionLease("aiko", tmp_path),
            )
            commands = [
                (
                    ["ticket", "comment", "--url", kwargs["ticket_url"]],
                    "Done.",
                ),
                (["task", "complete", "--status", "done"], "Fixed the login."),
            ]
            if linked_pr and not mention_only:
                commands.insert(
                    0,
                    (
                        [
                            "pr",
                            "create",
                            "--repo",
                            REPOSITORY,
                            "--head",
                            "ticket/T-1",
                            "--title",
                            "Fix login",
                            "--issue-url",
                            url,
                        ],
                        "Add the requested check.",
                    ),
                )
            for arguments, stdin in commands:
                # As the member broker runs it: on a worker thread of its own.
                code, stdout, stderr = await asyncio.to_thread(
                    member_cli.run_in_process,
                    [*arguments, "--person", "aiko", "--content-stdin"],
                    invocation,
                    cwd=tmp_path,
                    stdin=stdin,
                )
                assert (code, stderr) == (0, ""), stdout
                if arguments[:2] == ["pr", "create"]:
                    result = json.loads(stdout)
                    assert result["created"] is False
                    assert result["target"]["url"] == pr_url
            return AgentResponse(status=AgentResponse.DONE, message="done")

    async def run_workflow(invocation):
        nonlocal run
        run = invocation
        context = _Turn()
        context.shared_state[WORKFLOW_INVOCATION_KEY] = invocation
        return await ticket_driven_workflow.main(context)  # type: ignore[arg-type]

    run = None
    with trace_scope("routine", trace_id="run-1", person_id="aiko"):
        RunStore().start_record(
            "run-1",
            work_kind="ticket_mention" if mention_only else "ticket",
            execution_mode="autonomous",
            member_id="aiko",
        )
        response = await TicketSelector(_Selection()).run_next(  # type: ignore[arg-type]
            member.person, run_workflow
        )

    assert response.status == AgentResponse.DONE
    assert lanes == [Task.READY if mention_only and not linked_pr else Task.IN_PROGRESS]
    assert item(url)["assignees"] == ["yuki" if mention_only else "aiko"]
    assert run.payload["ticket_url"] == url
    assert item(url)["comments"][-1]["body"] == "Done."
    if linked_pr:
        assert len(item(url)["comments"]) == (2 if mention_only else 3)
        linked = await member.code.read("ticket_pull_requests", url)
        assert [p["url"] for p in linked.items] == [pr_url]
    assert RunStore().status("run-1").to_dict()["status"] == "done"
    assert RunStore().status("run-1").to_dict()["subject_url"] == url
    # Answered by the member, the ticket waits for someone else.
    assert await member.tickets.candidates() == []
