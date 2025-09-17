from pathlib import Path

import pytest

from guildbotics.entities.message import Message
from guildbotics.intelligences.common import AgentResponse
from guildbotics.modes.comment_mode import CommentMode


class StubGitTool:
    """Minimal stub for GitTool used by CommentMode."""

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

    def checkout_branch(self, branch_name: str):
        pass  # No-op for testing


class StubCodeHostingService:
    async def get_repository_url(self) -> str:
        return "https://example.com/org/repo.git"

    async def get_default_branch(self) -> str:
        return "main"


@pytest.mark.asyncio
async def test_run_returns_asking_and_uses_reply_as(monkeypatch, fake_context):
    """
    Verify CommentMode.run returns ASKING and relays a non-empty message
    produced via the reply_as function.

    The reply_as function is monkeypatched to avoid real model calls and to
    assert it receives the provided context and messages.
    """

    # Set up fake_context with required attributes and methods
    fake_context.task.id = "test-123"
    fake_context.task.repository = "test-repo"
    fake_context.person.account_info = {
        "git_user": "Test User",
        "git_email": "test@example.com",
    }

    def get_code_hosting_service(repository=None):
        return StubCodeHostingService()

    fake_context.get_code_hosting_service = get_code_hosting_service

    # Mock external dependencies
    monkeypatch.setattr("guildbotics.modes.mode_base.GitTool", StubGitTool)
    monkeypatch.setattr(
        "guildbotics.modes.mode_base.get_workspace_path",
        lambda person_id: Path("/tmp/fake_workspace"),
    )

    async def fake_reply_as(context, messages, repo_path):
        # Ensure the mode passes through the same context and messages
        assert context is fake_context
        assert isinstance(messages, list) and len(messages) == 1
        assert repo_path == Path("/tmp/fake_repo")  # Check repo_path from StubGitTool
        return "mocked reply"

    # Patch the symbol used inside comment_mode
    monkeypatch.setattr("guildbotics.modes.comment_mode.reply_as", fake_reply_as)

    mode = CommentMode(fake_context)
    messages = [Message(content="Hello", author="user", author_type=Message.USER)]

    res = await mode.run(messages)

    assert res.status == AgentResponse.ASKING
    assert isinstance(res.message, str) and res.message.strip() != ""
    assert res.message == "mocked reply"
