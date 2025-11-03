from pathlib import Path

import pytest

from guildbotics.entities.message import Message
from guildbotics.intelligences.common import AgentResponse
from guildbotics.templates.commands.workflows.modes import comment_mode


class StubGitTool:
    """Minimal stub for GitTool used by comment_mode."""

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


@pytest.mark.asyncio
async def test_run_returns_asking_and_uses_reply_as(monkeypatch, fake_context):
    """
    Verify comment_mode.main returns ASKING and relays a non-empty message
    produced via the reply_as function.

    The reply_as function is monkeypatched to avoid real model calls and to
    assert it receives the provided context and messages.
    """

    # Set up fake_context with required attributes
    fake_context.task.id = "test-123"
    fake_context.person.account_info = {
        "git_user": "Test User",
        "git_email": "test@example.com",
    }

    # Create a stub git_tool
    git_tool = StubGitTool(
        workspace=Path("/tmp/fake_workspace"),
        repo_url="https://example.com/org/repo.git",
        logger=fake_context.logger,
        user_name="Test User",
        user_email="test@example.com",
        default_branch="main",
    )

    async def fake_reply_as(context, messages, repo_path):
        # Ensure the mode passes through the same context and messages
        assert context is fake_context
        assert isinstance(messages, list) and len(messages) == 1
        assert repo_path == Path("/tmp/fake_repo")  # Check repo_path from StubGitTool
        return "mocked reply"

    # Patch the symbol used inside comment_mode
    monkeypatch.setattr(
        "guildbotics.templates.commands.workflows.modes.comment_mode.reply_as",
        fake_reply_as,
    )

    messages = [Message(content="Hello", author="user", author_type=Message.USER)]

    res = await comment_mode.main(fake_context, messages, git_tool)

    assert res.status == AgentResponse.ASKING
    assert isinstance(res.message, str) and res.message.strip() != ""
    assert res.message == "mocked reply"
