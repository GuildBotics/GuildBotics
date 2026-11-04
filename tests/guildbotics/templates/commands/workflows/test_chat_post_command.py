from __future__ import annotations

import types

import pytest

from guildbotics.commands.errors import CommandError
from guildbotics.templates.commands.workflows import chat_post_command
from guildbotics.runtime.port import ServiceError

POSTS = {}


class Chat:
    async def resolve_channel_id(self, name):
        return "dev-chat" if name.lstrip("#") == "dev-chat" else None

    async def post_message(self, channel_id, message, **kwargs):
        if channel_id == "../etc":
            raise ServiceError("Unsupported local chat channel")
        POSTS.setdefault(channel_id, []).append(message)


def _context(command_result: object = "hello") -> types.SimpleNamespace:
    """A command's context, whose chat is the local one with ``dev-chat``."""
    POSTS.clear()
    calls: list[tuple[str, tuple]] = []

    async def invoke(name: str, *args):
        calls.append((name, args))
        return command_result

    ctx = types.SimpleNamespace(invoke=invoke, get_chat_service=lambda: Chat())
    ctx._calls = calls
    return ctx


def _posts(channel_id: str) -> list[str]:
    return POSTS.get(channel_id, [])


@pytest.mark.asyncio
async def test_posts_using_explicit_channel_id():
    ctx = _context(command_result="daily summary")

    out = await chat_post_command.main(
        ctx, channel_id="dev-chat", command="examples/reports/morning_summary"
    )

    assert out == "daily summary"
    assert ctx._calls == [("examples/reports/morning_summary", ())]
    assert _posts("dev-chat") == ["daily summary"]


@pytest.mark.asyncio
async def test_resolves_channel_name_when_channel_id_missing():
    ctx = _context(command_result="digest")

    out = await chat_post_command.main(
        ctx,
        channel_name="#dev-chat",
        command='examples/reports/ai_news_digest query="OpenAI"',
    )

    assert out == "digest"
    assert ctx._calls == [("examples/reports/ai_news_digest", ("query=OpenAI",))]
    assert _posts("dev-chat") == ["digest"]


@pytest.mark.asyncio
async def test_skips_when_command_output_is_empty():
    ctx = _context(command_result=None)

    out = await chat_post_command.main(
        ctx, channel_id="dev-chat", command="examples/reports/morning_summary"
    )

    assert out == ""
    assert len(ctx._calls) == 1
    assert _posts("dev-chat") == []


@pytest.mark.parametrize(
    ("channel", "command", "message"),
    [
        pytest.param(
            {},
            "examples/reports/morning_summary",
            "channel_id or channel_name is required",
            id="no-channel",
        ),
        pytest.param(
            {"channel_name": "missing"},
            "examples/reports/morning_summary",
            "Chat channel was not found: missing",
            id="unresolved-channel-name",
        ),
        pytest.param(
            {"channel_id": "dev-chat"},
            'examples/reports/ai_news_digest query="OpenAI',
            "Invalid command syntax",
            id="invalid-quotes",
        ),
        pytest.param(
            {"channel_id": "dev-chat"},
            "  ",
            "A command to post is required",
            id="empty-command",
        ),
    ],
)
@pytest.mark.asyncio
async def test_fails_without_running_or_posting_when_it_cannot_post(
    channel: dict[str, str], command: str, message: str
):
    ctx = _context(command_result="digest")

    with pytest.raises(CommandError, match=message):
        await chat_post_command.main(ctx, command=command, **channel)

    assert ctx._calls == []
    assert _posts("dev-chat") == []


@pytest.mark.asyncio
async def test_a_chat_failure_is_the_commands_failure():
    ctx = _context(command_result="digest")

    with pytest.raises(CommandError, match="Unsupported local chat channel"):
        await chat_post_command.main(ctx, channel_id="../etc", command="print")
