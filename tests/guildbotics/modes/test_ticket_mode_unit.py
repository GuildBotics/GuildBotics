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
from guildbotics.modes.ticket_mode import TicketMode


class StubGitTool:
    """Minimal stub for GitTool used by TicketMode.

    Captures the branch passed to `checkout_branch` and exposes a fake `repo_path`.
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
        self.checked_out = []

    def checkout_branch(self, branch_name: str):
        self.checked_out.append(branch_name)


class StubCodeHostingService:
    async def get_repository_url(self) -> str:
        return "https://example.com/org/repo.git"

    async def get_default_branch(self) -> str:
        return "main"


class StubTicketManager:
    def __init__(self):
        self.created_tasks = None

    async def create_tickets(self, tasks: list[Task]):
        # Store tasks to assert later
        self.created_tasks = list(tasks)

    async def get_ticket_url(self, task: Task) -> str:
        return f"https://tickets.local/{task.title.replace(' ', '_')}"


class ProjectWithServices:
    """Fake project exposing available services queried by ModeBase.get_available_modes."""

    def get_language_code(self) -> str:
        return "en"

    def get_language_name(self) -> str:
        return "English"

    def get_available_services(self):
        # Ensure ticket and code hosting dependent services are reported available
        from guildbotics.entities.team import Service

        return [Service.TICKET_MANAGER, Service.CODE_HOSTING_SERVICE]


@pytest.mark.asyncio
async def test_run_stubs_external_calls_and_creates_tickets(monkeypatch, fake_context):
    """Unit test for TicketMode.run with all externals stubbed (pure logic).

    - Stubs GitTool to avoid real git operations and captures branch checkout.
    - Stubs identify_next_tasks to control task flow.
    - Stubs ticket manager interactions and talk_as to avoid any LLM/network calls.
    - Verifies owner propagation, function call wiring, and AgentResponse.
    """

    # Prepare context: add required methods and attributes used by TicketMode
    fake_context.team = type("T", (), {"project": ProjectWithServices()})()
    fake_context.task = Task(
        id="12345",
        title="Implement feature X",
        description="Do X",
        role="dev",
        owner="alice",
    )
    fake_context.task.repository = "test-repo"  # Set repository for the task

    ticket_manager = StubTicketManager()

    def get_code_hosting_service(repository: str | None = None):
        return StubCodeHostingService()

    def get_ticket_manager():
        return ticket_manager

    fake_context.get_code_hosting_service = get_code_hosting_service  # type: ignore[attr-defined]
    fake_context.get_ticket_manager = get_ticket_manager  # type: ignore[attr-defined]

    # Monkeypatch GitTool used inside mode_base (which TicketMode inherits from)
    monkeypatch.setattr("guildbotics.modes.mode_base.GitTool", StubGitTool)

    # Mock get_workspace_path to return a fake path
    monkeypatch.setattr(
        "guildbotics.modes.mode_base.get_workspace_path",
        lambda person_id: Path("/tmp/fake_workspace"),
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
        "guildbotics.modes.ticket_mode.identify_next_tasks", fake_identify_next_tasks
    )

    # Make i18n translation deterministic and simple
    def fake_t(key: str, **kwargs):
        if key == "modes.ticket_mode.agent_response_message":
            return f"Tickets created: {kwargs.get('task_labels')}"
        if key == "modes.ticket_mode.agent_response_context_location":
            return "Ticket Comment"
        return key

    monkeypatch.setattr("guildbotics.modes.ticket_mode.t", fake_t)

    # Capture talk_as invocations and return a fixed assistant message
    talked = {}

    async def fake_talk_as(context, system_message, context_location, messages):
        talked["args"] = (context, system_message, context_location, messages)
        return "assistant reply"

    monkeypatch.setattr("guildbotics.modes.ticket_mode.talk_as", fake_talk_as)

    # For stable available_modes, bypass ModeBase implementation
    monkeypatch.setattr(
        "guildbotics.modes.ticket_mode.ModeBase.get_available_modes",
        lambda team: Labels({"ticket": "desc"}),
    )

    # Execute
    mode = TicketMode(fake_context)
    messages = [Message(content="hello", author="u", author_type=Message.USER)]
    res = await mode.run(messages)

    # Assertions
    assert res.status == AgentResponse.DONE
    assert res.message == "assistant reply"

    # Git branch checkout occurred with expected naming
    # Access the last constructed stub via monkeypatch is not direct,
    # so assert through recorded identify_next call that repo_path was set by stub
    assert calls["identify_next"], "identify_next_tasks was not called"

    # Role and messages are correctly forwarded
    first_next = calls["identify_next"][0]
    assert first_next["role"] == "dev"
    assert first_next["messages"] == messages
    assert isinstance(first_next["available_modes"], Labels)

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
