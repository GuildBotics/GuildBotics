from __future__ import annotations

import datetime as dt
import types

import pytest

from guildbotics.integrations.chat_service import ChatPostResult
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.templates.commands.workflows.chat import chat_scheduled_post_workflow


class FakeChatService:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str | None]] = []
        self.channel_name_map: dict[str, str] = {}

    async def resolve_channel_id(self, channel_name: str) -> str | None:
        return self.channel_name_map.get(channel_name)

    async def post_message(
        self, channel_id: str, text: str, *, thread_ts: str | None = None
    ):
        self.posts.append((channel_id, text, thread_ts))
        return ChatPostResult(
            channel_id=channel_id, message_ts="200.1", thread_ts="200.1"
        )


class StubLogger:
    def __init__(self) -> None:
        self.lines: list[tuple] = []

    def info(self, *args, **kwargs):
        self.lines.append(args)


def _context(command_result: object = "hello") -> types.SimpleNamespace:
    person = types.SimpleNamespace(
        person_id="alice",
        profile={
            "chat": {
                "scheduled_posts": [
                    {
                        "name": "morning-topic",
                        "service": "slack",
                        "channel_id": "C1",
                        "cron": "0 9 * * 1-5",
                        "command": "examples/reports/morning_summary",
                        "enabled": True,
                    }
                ]
            }
        },
    )
    calls: list[tuple[str, tuple]] = []

    async def invoke(name: str, *args):
        calls.append((name, args))
        return command_result

    ctx = types.SimpleNamespace(person=person, logger=StubLogger(), invoke=invoke)
    ctx._invoke_calls = calls
    return ctx


def _context_channel_name_post(
    command_result: object = "hello",
) -> types.SimpleNamespace:
    person = types.SimpleNamespace(
        person_id="alice",
        profile={
            "chat": {
                "scheduled_posts": [
                    {
                        "name": "morning-topic",
                        "service": "slack",
                        "channel_name": "dev-chat",
                        "cron": "0 9 * * 1-5",
                        "command": "examples/reports/morning_summary",
                        "enabled": True,
                    }
                ]
            }
        },
    )
    calls: list[tuple[str, tuple]] = []

    async def invoke(name: str, *args):
        calls.append((name, args))
        return command_result

    ctx = types.SimpleNamespace(person=person, logger=StubLogger(), invoke=invoke)
    ctx._invoke_calls = calls
    return ctx


@pytest.mark.asyncio
async def test_posts_when_due_and_records_slot(tmp_path):
    ctx = _context(command_result="daily summary")
    svc = FakeChatService()
    store = FileConversationStateStore(base_dir=tmp_path)
    now = dt.datetime(2026, 2, 23, 9, 0, 5)  # Monday

    await chat_scheduled_post_workflow.main(
        ctx, chat_service=svc, state_store=store, now=now
    )

    assert ctx._invoke_calls == [("examples/reports/morning_summary", ())]
    assert svc.posts == [("C1", "daily summary", None)]

    state = store.load_scheduled_post_state("slack", "alice", "morning-topic")
    assert state.last_run_slot == "2026-02-23T09:00"


@pytest.mark.asyncio
async def test_skips_duplicate_post_in_same_minute(tmp_path):
    ctx = _context(command_result="daily summary")
    svc = FakeChatService()
    store = FileConversationStateStore(base_dir=tmp_path)
    now = dt.datetime(2026, 2, 23, 9, 0, 10)

    await chat_scheduled_post_workflow.main(
        ctx, chat_service=svc, state_store=store, now=now
    )
    await chat_scheduled_post_workflow.main(
        ctx, chat_service=svc, state_store=store, now=now
    )

    assert len(ctx._invoke_calls) == 1
    assert len(svc.posts) == 1


@pytest.mark.asyncio
async def test_skips_when_not_due(tmp_path):
    ctx = _context(command_result="daily summary")
    svc = FakeChatService()
    store = FileConversationStateStore(base_dir=tmp_path)
    now = dt.datetime(2026, 2, 23, 9, 1, 0)

    await chat_scheduled_post_workflow.main(
        ctx, chat_service=svc, state_store=store, now=now
    )

    assert ctx._invoke_calls == []
    assert svc.posts == []


@pytest.mark.asyncio
async def test_command_output_is_stringified_and_empty_output_skips_post(tmp_path):
    ctx = _context(command_result=None)
    svc = FakeChatService()
    store = FileConversationStateStore(base_dir=tmp_path)
    now = dt.datetime(2026, 2, 23, 9, 0, 0)

    await chat_scheduled_post_workflow.main(
        ctx, chat_service=svc, state_store=store, now=now
    )

    # command is still executed and slot is marked to prevent re-post loop this minute
    assert len(ctx._invoke_calls) == 1
    assert svc.posts == []
    state = store.load_scheduled_post_state("slack", "alice", "morning-topic")
    assert state.last_run_slot == "2026-02-23T09:00"


@pytest.mark.asyncio
async def test_resolves_channel_name_when_channel_id_missing(tmp_path):
    ctx = _context_channel_name_post(command_result="daily summary")
    svc = FakeChatService()
    svc.channel_name_map["dev-chat"] = "C1"
    store = FileConversationStateStore(base_dir=tmp_path)
    now = dt.datetime(2026, 2, 23, 9, 0, 5)

    await chat_scheduled_post_workflow.main(
        ctx, chat_service=svc, state_store=store, now=now
    )

    assert svc.posts == [("C1", "daily summary", None)]


@pytest.mark.asyncio
async def test_logs_and_skips_when_scheduled_command_has_invalid_quotes(tmp_path):
    ctx = _context(command_result="daily summary")
    ctx.person.profile["chat"]["scheduled_posts"][0]["command"] = (
        'reports/x query="OpenAI'
    )
    svc = FakeChatService()
    store = FileConversationStateStore(base_dir=tmp_path)
    now = dt.datetime(2026, 2, 23, 9, 0, 5)

    await chat_scheduled_post_workflow.main(
        ctx, chat_service=svc, state_store=store, now=now
    )

    assert ctx._invoke_calls == []
    assert svc.posts == []
    state = store.load_scheduled_post_state("slack", "alice", "morning-topic")
    assert state.last_run_slot == "2026-02-23T09:00"
    assert any("invalid command syntax" in str(line[0]) for line in ctx.logger.lines)


@pytest.mark.asyncio
async def test_skips_invalid_cron_entry_and_continues_other_scheduled_posts(tmp_path):
    ctx = _context(command_result="daily summary")
    ctx.person.profile["chat"]["scheduled_posts"] = [
        {
            "name": "broken",
            "service": "slack",
            "channel_id": "C1",
            "cron": "not a cron",
            "command": "reports/invalid",
            "enabled": True,
        },
        {
            "name": "morning-topic",
            "service": "slack",
            "channel_id": "C1",
            "cron": "0 9 * * 1-5",
            "command": "examples/reports/morning_summary",
            "enabled": True,
        },
    ]
    svc = FakeChatService()
    store = FileConversationStateStore(base_dir=tmp_path)
    now = dt.datetime(2026, 2, 23, 9, 0, 5)

    await chat_scheduled_post_workflow.main(
        ctx, chat_service=svc, state_store=store, now=now
    )

    assert ctx._invoke_calls == [("examples/reports/morning_summary", ())]
    assert svc.posts == [("C1", "daily summary", None)]
    assert any("invalid cron expression" in str(line[0]) for line in ctx.logger.lines)
