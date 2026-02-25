from __future__ import annotations

import types

import pytest

from guildbotics.drivers.chat_event_source import PollingChatEventSource
from guildbotics.integrations.chat_service import (
    ChatEvent,
    ChatEventPage,
    ChatIdentity,
    ChatPostResult,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.templates.commands.workflows import chat_conversation_workflow


class StubLogger:
    def __init__(self) -> None:
        self.lines: list[tuple] = []

    def info(self, *args):
        self.lines.append(args)


class FakeChatService:
    def __init__(self) -> None:
        self.identity = ChatIdentity(user_id="U_ALICE", display_name="AliceBot")
        self.pages: dict[str, ChatEventPage] = {}
        self.posts: list[tuple[str, str, str | None]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.channel_name_map: dict[str, str] = {}

    async def get_bot_identity(self) -> ChatIdentity:
        return self.identity

    async def list_channel_events(
        self, channel_id: str, *, cursor=None, oldest_ts=None, limit: int = 100
    ) -> ChatEventPage:
        return self.pages.get(channel_id, ChatEventPage())

    async def resolve_channel_id(self, channel_name: str) -> str | None:
        return self.channel_name_map.get(channel_name)

    async def post_message(
        self, channel_id: str, text: str, *, thread_ts: str | None = None
    ) -> ChatPostResult:
        self.posts.append((channel_id, text, thread_ts))
        ts = "999.1"
        return ChatPostResult(channel_id=channel_id, message_ts=ts, thread_ts=thread_ts or ts)

    async def add_reaction(self, channel_id: str, message_ts: str, reaction: str) -> None:
        self.reactions.append((channel_id, message_ts, reaction))


class FakeInvokeContext(types.SimpleNamespace):
    def __init__(self, *, person, logger, reply_text: str) -> None:
        super().__init__(person=person, logger=logger)
        self.pipe = ""
        self.shared_state: dict[str, object] = {}
        self._reply_text = reply_text
        self.invocations: list[str] = []
        self.last_chat_reply_input = None
        self.last_pipe = ""

    async def invoke(self, name: str, /, *args, **kwargs):
        self.invocations.append(name)
        self.last_chat_reply_input = self.shared_state.get("chat_reply_input")
        self.last_pipe = self.pipe
        return self._reply_text


class CrashAfterPostStateStore(FileConversationStateStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._crashed = False

    def save_thread_state(self, service, person_id, channel_id, thread_ts, state):
        if not self._crashed and getattr(state, "last_bot_replier_id", None):
            self._crashed = True
            raise RuntimeError("simulated crash after post")
        return super().save_thread_state(service, person_id, channel_id, thread_ts, state)


def _context_with_chat_profile() -> types.SimpleNamespace:
    person = types.SimpleNamespace(
        person_id="alice",
        name="Alice",
        profile={
            "chat": {
                "subscriptions": [
                    {"service": "slack", "channel_id": "C1", "enabled": True}
                ]
            }
        },
    )
    return types.SimpleNamespace(person=person, logger=StubLogger())


def _context_with_channel_name_subscription() -> types.SimpleNamespace:
    person = types.SimpleNamespace(
        person_id="alice",
        name="Alice",
        profile={
            "chat": {
                "subscriptions": [
                    {"service": "slack", "channel_name": "dev-chat", "enabled": True}
                ]
            }
        },
    )
    return types.SimpleNamespace(person=person, logger=StubLogger())


def _invoke_context_with_chat_profile(reply_text: str = "LLM reply") -> FakeInvokeContext:
    person = types.SimpleNamespace(
        person_id="alice",
        name="Alice",
        profile={
            "chat": {
                "subscriptions": [
                    {"service": "slack", "channel_id": "C1", "enabled": True}
                ]
            }
        },
    )
    return FakeInvokeContext(person=person, logger=StubLogger(), reply_text=reply_text)


@pytest.mark.asyncio
async def test_workflow_replies_to_explicit_mention_and_updates_state(tmp_path):
    service = FakeChatService()
    service.pages["C1"] = ChatEventPage(
        events=[
            ChatEvent(
                event_id="E1",
                channel_id="C1",
                message_ts="100.1",
                thread_ts="100.1",
                author_id="U_USER",
                text="<@U_ALICE> please check this",
                mentions=["U_ALICE"],
                is_thread_reply=False,
            )
        ],
        cursor="next-cursor",
        oldest_ts="100.1",
    )
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _context_with_chat_profile()

    await chat_conversation_workflow.main(
        ctx,
        chat_service=service,
        state_store=state_store,
        event_source=PollingChatEventSource(chat_service=service, state_store=state_store),
    )

    assert len(service.posts) == 1
    channel_id, text, thread_ts = service.posts[0]
    assert channel_id == "C1"
    assert thread_ts == "100.1"
    assert "Alice:" in text

    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.cursor == "next-cursor"
    assert channel_state.oldest_ts == "100.1"
    assert "E1" in channel_state.processed_event_ids

    thread_state = state_store.load_thread_state("slack", "alice", "C1", "100.1")
    assert "alice" in thread_state.participants
    assert thread_state.last_bot_replier_id == "alice"
    assert thread_state.response_expected is True
    thread_messages = state_store.load_thread_messages("slack", "alice", "C1", "100.1")
    assert [m.message_ts for m in thread_messages] == ["100.1", "999.1"]
    assert thread_messages[0].is_bot_message is False
    assert thread_messages[1].is_bot_message is True


@pytest.mark.asyncio
async def test_workflow_uses_chat_reply_command_when_invoker_is_available(tmp_path):
    service = FakeChatService()
    service.pages["C1"] = ChatEventPage(
        events=[
            ChatEvent(
                event_id="E1",
                channel_id="C1",
                message_ts="100.1",
                thread_ts="100.1",
                author_id="U_USER",
                text="<@U_ALICE> explain this error",
                mentions=["U_ALICE"],
                is_thread_reply=False,
            )
        ]
    )
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="確認します。まずエラーログを共有してください。")

    await chat_conversation_workflow.main(
        ctx,
        chat_service=service,
        state_store=state_store,
        event_source=PollingChatEventSource(chat_service=service, state_store=state_store),
    )

    assert ctx.invocations == ["workflows/chat_reply"]
    assert "chat_reply_input" not in ctx.shared_state
    assert ctx.pipe == ""
    assert ctx.last_chat_reply_input is not None
    assert "thread_messages" in ctx.last_chat_reply_input
    assert "explain this error" in ctx.last_pipe
    assert service.posts[0][1] == "確認します。まずエラーログを共有してください。"


@pytest.mark.asyncio
async def test_workflow_ignores_non_trigger_and_marks_processed(tmp_path):
    service = FakeChatService()
    service.pages["C1"] = ChatEventPage(
        events=[
            ChatEvent(
                event_id="E2",
                channel_id="C1",
                message_ts="101.1",
                thread_ts="101.1",
                author_id="U_USER",
                text="hello all",
                mentions=[],
                is_thread_reply=False,
            )
        ]
    )

    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _context_with_chat_profile()

    await chat_conversation_workflow.main(
        ctx,
        chat_service=service,
        state_store=state_store,
        event_source=PollingChatEventSource(chat_service=service, state_store=state_store),
    )

    assert service.posts == []
    assert service.reactions == []
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert "E2" in channel_state.processed_event_ids


@pytest.mark.asyncio
async def test_workflow_resolves_channel_name_when_channel_id_missing(tmp_path):
    service = FakeChatService()
    service.channel_name_map["dev-chat"] = "C1"
    service.pages["C1"] = ChatEventPage(
        events=[
            ChatEvent(
                event_id="E3",
                channel_id="C1",
                message_ts="102.1",
                thread_ts="102.1",
                author_id="U_USER",
                text="<@U_ALICE> ping",
                mentions=["U_ALICE"],
                is_thread_reply=False,
            )
        ]
    )
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _context_with_channel_name_subscription()

    await chat_conversation_workflow.main(
        ctx,
        chat_service=service,
        state_store=state_store,
        event_source=PollingChatEventSource(chat_service=service, state_store=state_store),
    )

    assert len(service.posts) == 1
    assert service.posts[0][0] == "C1"


@pytest.mark.asyncio
async def test_workflow_does_not_duplicate_reply_after_crash_before_mark_processed(tmp_path):
    service = FakeChatService()
    service.pages["C1"] = ChatEventPage(
        events=[
            ChatEvent(
                event_id="E4",
                channel_id="C1",
                message_ts="103.1",
                thread_ts="103.1",
                author_id="U_USER",
                text="<@U_ALICE> retry-safe?",
                mentions=["U_ALICE"],
                is_thread_reply=False,
            )
        ]
    )
    state_store = CrashAfterPostStateStore(base_dir=tmp_path)
    ctx = _context_with_chat_profile()
    source = PollingChatEventSource(chat_service=service, state_store=state_store)

    with pytest.raises(RuntimeError, match="simulated crash after post"):
        await chat_conversation_workflow.main(
            ctx,
            chat_service=service,
            state_store=state_store,
            event_source=source,
        )
    assert len(service.posts) == 1
    # Side effect success is recorded before the simulated crash.
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert "E4" in channel_state.processed_event_ids

    # Retry should not create a duplicate reply for the same event.
    await chat_conversation_workflow.main(
        ctx,
        chat_service=service,
        state_store=state_store,
        event_source=PollingChatEventSource(chat_service=service, state_store=state_store),
    )
    assert len(service.posts) == 1
