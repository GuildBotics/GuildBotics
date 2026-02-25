from __future__ import annotations

import types

import pytest

from guildbotics.integrations.chat_service import ChatPostResult
from guildbotics.templates.commands.workflows import chat_post_command


class FakeChatService:
    def __init__(self) -> None:
        self.channel_name_map: dict[str, str] = {}
        self.posts: list[tuple[str, str, str | None]] = []

    async def resolve_channel_id(self, channel_name: str) -> str | None:
        return self.channel_name_map.get(channel_name)

    async def post_message(
        self, channel_id: str, text: str, *, thread_ts: str | None = None
    ) -> ChatPostResult:
        self.posts.append((channel_id, text, thread_ts))
        return ChatPostResult(channel_id=channel_id, message_ts="200.1", thread_ts="200.1")


class StubLogger:
    def __init__(self) -> None:
        self.lines: list[tuple] = []

    def info(self, *args):
        self.lines.append(args)


def _context(command_result: object = "hello") -> types.SimpleNamespace:
    svc = FakeChatService()
    calls: list[tuple[str, tuple]] = []

    async def invoke(name: str, *args):
        calls.append((name, args))
        return command_result

    ctx = types.SimpleNamespace(
        logger=StubLogger(),
        invoke=invoke,
        get_chat_service=lambda: svc,
    )
    ctx._svc = svc
    ctx._calls = calls
    return ctx


@pytest.mark.asyncio
async def test_posts_using_explicit_channel_id():
    ctx = _context(command_result="daily summary")

    out = await chat_post_command.main(
        ctx,
        service="slack",
        channel_id="C1",
        command="reports/morning_summary",
    )

    assert out == "daily summary"
    assert ctx._calls == [("reports/morning_summary", ())]
    assert ctx._svc.posts == [("C1", "daily summary", None)]


@pytest.mark.asyncio
async def test_resolves_channel_name_when_channel_id_missing():
    ctx = _context(command_result="digest")
    ctx._svc.channel_name_map["dev-chat"] = "C2"

    out = await chat_post_command.main(
        ctx,
        service="slack",
        channel_name="dev-chat",
        command='reports/ai_news_digest query="OpenAI"',
    )

    assert out == "digest"
    assert ctx._calls == [("reports/ai_news_digest", ("query=OpenAI",))]
    assert ctx._svc.posts == [("C2", "digest", None)]


@pytest.mark.asyncio
async def test_skips_when_command_output_is_empty():
    ctx = _context(command_result=None)

    out = await chat_post_command.main(
        ctx,
        service="slack",
        channel_id="C1",
        command="reports/morning_summary",
    )

    assert out == ""
    assert len(ctx._calls) == 1
    assert ctx._svc.posts == []


@pytest.mark.asyncio
async def test_logs_and_skips_when_channel_name_cannot_be_resolved():
    ctx = _context(command_result="digest")

    out = await chat_post_command.main(
        ctx,
        service="slack",
        channel_name="missing",
        command="reports/morning_summary",
    )

    assert out == ""
    assert ctx._svc.posts == []
    assert ctx._calls == []
    assert any("could not be resolved" in str(line[0]) for line in ctx.logger.lines)
