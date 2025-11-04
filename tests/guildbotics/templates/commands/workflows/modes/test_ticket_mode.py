from pathlib import Path

import pytest

from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.intelligences.common import (
    AgentResponse,
    Labels,
    NextTaskItem,
    NextTasksResponse,
)
from guildbotics.templates.commands.workflows.modes import ticket_mode


class StubGitTool:
    """Minimal stub for GitTool used by ticket_mode.

    Exposes a fake `repo_path` for testing.
    """

    def __init__(
        self, workspace, repo_url, logger, user_name, user_email, default_branch
    ):
        self.workspace = workspace
        self.repo_url = repo_url
        self.logger = logger
        self.user_name = user_name
        self.user_email = user_email
        self.default_branch = default_branch
        self.repo_path = Path("/tmp/fake_repo")


class StubTicketManager:
    """Stub TicketManager for testing ticket_mode."""

    def __init__(self):
        self.created_tasks = None

    async def create_tickets(self, tasks: list[Task]):
        # Store tasks to assert later
        self.created_tasks = list(tasks)

    async def get_ticket_url(self, task: Task) -> str:
        return f"https://tickets.local/{task.title.replace(' ', '_')}"


@pytest.mark.asyncio
async def test_run_stubs_external_calls_and_creates_tickets(monkeypatch, fake_context):
    """Unit test for ticket_mode.main with all externals stubbed (pure logic).

    - Stubs GitTool to avoid real git operations and captures branch checkout.
    - Stubs identify_next_tasks to control task flow.
    - Stubs ticket manager interactions and talk_as to avoid any LLM/network calls.
    - Verifies owner propagation, function call wiring, and AgentResponse.
    """

    # Prepare context: add required methods and attributes used by ticket_mode
    fake_context.task = Task(
        id="12345",
        title="Implement feature X",
        description="Do X",
        role="dev",
        owner="alice",
    )

    ticket_manager = StubTicketManager()
    git_tool = StubGitTool(
        workspace=Path("/tmp/fake_workspace"),
        repo_url="https://example.com/org/repo.git",
        logger=None,
        user_name="testuser",
        user_email="test@example.com",
        default_branch="main",
    )

    # Track calls and inputs to the identify functions
    calls = {"identify_next": []}

    next_items = [
        NextTaskItem(
            title="Task A",
            description="Desc A",
            role="dev",
            priority=1,
            inputs=["spec"],
            output="artifact",
            mode="ticket",
        )
    ]

    async def fake_identify_next_tasks(
        context, role, repo_path, messages, available_modes
    ):
        # Record call for assertion
        calls["identify_next"].append(
            {
                "context": context,
                "role": role,
                "repo_path": repo_path,
                "messages": messages,
                "available_modes": available_modes,
            }
        )
        return NextTasksResponse(tasks=list(next_items))

    # Patch functions referenced within ticket_mode
    monkeypatch.setattr(
        "guildbotics.templates.commands.workflows.modes.ticket_mode.identify_next_tasks",
        fake_identify_next_tasks,
    )

    # Make i18n translation deterministic and simple
    def fake_t(key: str, **kwargs):
        if key == "modes.ticket_mode.agent_response_message":
            return f"Tickets created: {kwargs.get('task_labels')}"
        if key == "modes.ticket_mode.agent_response_context_location":
            return "Ticket Comment"
        return key

    monkeypatch.setattr(
        "guildbotics.templates.commands.workflows.modes.ticket_mode.t", fake_t
    )

    # Capture talk_as invocations and return a fixed assistant message
    talked = {}

    async def fake_talk_as(context, system_message, context_location, messages):
        talked["args"] = (context, system_message, context_location, messages)
        return "assistant reply"

    monkeypatch.setattr(
        "guildbotics.templates.commands.workflows.modes.ticket_mode.talk_as",
        fake_talk_as,
    )

    # For stable available_modes, bypass Task.get_available_modes
    monkeypatch.setattr(
        "guildbotics.entities.Task.get_available_modes",
        lambda: {"ticket": "desc"},
    )

    # Execute the main function directly
    messages = [Message(content="hello", author="u", author_type=Message.USER)]
    res = await ticket_mode.main(fake_context, messages, git_tool, ticket_manager)

    # Assertions
    assert res.status == AgentResponse.DONE
    assert res.message == "assistant reply"

    # Verify identify_next_tasks was called
    assert calls["identify_next"], "identify_next_tasks was not called"

    # Role and messages are correctly forwarded
    first_next = calls["identify_next"][0]
    assert first_next["role"] == "dev"
    assert first_next["messages"] == messages
    assert isinstance(first_next["available_modes"], Labels)
    # Verify repo_path from git_tool was passed
    assert first_next["repo_path"] == git_tool.repo_path

    # Ticket manager received tasks with propagated owner
    assert ticket_manager.created_tasks is not None
    owners = {t.owner for t in ticket_manager.created_tasks}
    titles = {t.title for t in ticket_manager.created_tasks}
    assert owners == {"alice"}
    assert titles == {"Task A"}

    # talk_as was invoked with our translated context location and a system message
    ctx, system_message, context_location, _ = talked["args"]
    assert ctx is fake_context
    assert context_location == "Ticket Comment"
    assert "Tickets created:" in system_message
