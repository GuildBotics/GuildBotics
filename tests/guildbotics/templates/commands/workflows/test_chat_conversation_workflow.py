from __future__ import annotations

import types

import pytest

from guildbotics.entities.message import Message
from guildbotics.integrations.chat_state_store import ThreadMessageState
from guildbotics.integrations.chat_service import (
    ChatEvent,
    ChatIdentity,
    ChatPostResult,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.runtime.event_listener import INCOMING_CHAT_EVENT_KEY, IncomingChatEvent
from guildbotics.templates.commands.workflows import chat_conversation_workflow


class StubLogger:
    def __init__(self) -> None:
        self.lines: list[tuple] = []

    def info(self, *args):
        self.lines.append(args)


class FakeChatService:
    def __init__(self) -> None:
        self.identity = ChatIdentity(user_id="U_ALICE", display_name="AliceBot")
        self.posts: list[tuple[str, str, str | None]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.channel_name_map: dict[str, str] = {}

    async def get_bot_identity(self) -> ChatIdentity:
        return self.identity

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

    def normalize_participant_text(
        self, text: str, participant_labels: dict[str, str]
    ) -> str:
        for user_id, label in participant_labels.items():
            text = text.replace(f"<@{user_id}>", f"@{label}")
        return text

    def render_participant_text(
        self, text: str, participant_labels: dict[str, str]
    ) -> str:
        for user_id, label in participant_labels.items():
            text = text.replace(f"@{label}", f"<@{user_id}>")
        return text


class FakeInvokeContext(types.SimpleNamespace):
    def __init__(
        self,
        *,
        person,
        logger,
        reply_text: str,
    ) -> None:
        super().__init__(person=person, logger=logger, language_code="ja")
        self.pipe = ""
        self.shared_state: dict[str, object] = {}
        self._reply_text = reply_text
        self._reply_intent = {
            "label": "answer",
            "reason": "default",
            "confidence": 0.9,
        }
        self._thread_context = {
            "thread_topic": "thread topic",
            "latest_focus": "latest focus",
            "reason": "default",
            "confidence": 0.9,
        }
        self.invocations: list[tuple[str, tuple, dict]] = []
        self.last_chat_reply_input = None
        self.last_pipe = ""

    async def invoke(self, name: str, /, *args, **kwargs):
        self.invocations.append((name, args, kwargs))
        self.last_chat_reply_input = self.shared_state.get("chat_reply_input")
        self.last_pipe = self.pipe
        if name == "workflows/chat/should_react":
            return {
                "decision": "reply",
                "reason": "test",
                "reaction": None,
            }
        if name == "workflows/chat/chat_thread_context":
            return self._thread_context
        if name == "workflows/chat/chat_reply_intent":
            return self._reply_intent
        if name == "workflows/chat/chat_reply_actionable":
            return self._reply_text
        return self._reply_text


class RuntimeLookupContext(FakeInvokeContext):
    def __init__(self, *, person, logger, reply_text: str) -> None:
        super().__init__(person=person, logger=logger, reply_text=reply_text)
        self._identity_map: dict[str, str] = {}

    def clone_for(self, person):
        user_id = self._identity_map.get(getattr(person, "person_id", ""), "")

        class _Service:
            async def get_bot_identity(self_nonlocal):
                return ChatIdentity(user_id=user_id, display_name=getattr(person, "name", ""))

        clone = types.SimpleNamespace(
            person=person,
            get_chat_service=lambda: _Service(),
        )

        async def _aclose():
            return None

        clone.aclose = _aclose
        return clone


class CrashAfterPostStateStore(FileConversationStateStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._crashed = False

    def save_thread_state(self, service, person_id, channel_id, thread_ts, state):
        if not self._crashed and getattr(state, "participants", None):
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
    return types.SimpleNamespace(person=person, logger=StubLogger(), language_code="ja")


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
    return types.SimpleNamespace(person=person, logger=StubLogger(), language_code="ja")


def _invoke_context_with_chat_profile(
    reply_text: str = "LLM reply",
) -> FakeInvokeContext:
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
    return FakeInvokeContext(
        person=person,
        logger=StubLogger(),
        reply_text=reply_text,
    )


def _set_incoming_event(
    ctx: types.SimpleNamespace,
    *,
    event_id: str,
    channel_id: str = "C1",
    message_ts: str,
    thread_ts: str | None = None,
    author_id: str | None = "U_USER",
    text: str,
    mentions: list[str] | None = None,
    is_thread_reply: bool = False,
    is_edit_or_delete: bool = False,
) -> None:
    ctx.shared_state = {
        INCOMING_CHAT_EVENT_KEY: IncomingChatEvent(
            service_name="slack",
            channel_id=channel_id,
            event=ChatEvent(
                event_id=event_id,
                channel_id=channel_id,
                message_ts=message_ts,
                thread_ts=thread_ts or message_ts,
                author_id=author_id,
                text=text,
                mentions=list(mentions or []),
                is_edit_or_delete=is_edit_or_delete,
                is_thread_reply=is_thread_reply,
            ),
        ).to_shared_state()
    }


@pytest.mark.asyncio
async def test_workflow_replies_to_explicit_mention_and_updates_state(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _context_with_chat_profile()
    _set_incoming_event(
        ctx,
        event_id="E1",
        message_ts="100.1",
        text="<@U_ALICE> please check this",
        mentions=["U_ALICE"],
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert len(service.posts) == 1
    channel_id, text, thread_ts = service.posts[0]
    assert channel_id == "C1"
    assert thread_ts == "100.1"
    assert (
        text
        == chat_conversation_workflow.t(
            "commands.workflows.chat_conversation_workflow.reply_generation_failed"
        )
    )

    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert "E1" in channel_state.processed_event_ids

    thread_state = state_store.load_thread_state("slack", "alice", "C1", "100.1")
    assert "alice" in thread_state.participants
    thread_messages = state_store.load_thread_messages("slack", "alice", "C1", "100.1")
    assert [m.message_ts for m in thread_messages] == ["100.1", "999.1"]
    assert thread_messages[0].is_bot_message is False
    assert thread_messages[1].is_bot_message is True


@pytest.mark.asyncio
async def test_workflow_uses_chat_reply_command_when_invoker_is_available(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="確認します。まずエラーログを共有してください。")
    _set_incoming_event(
        ctx,
        event_id="E1",
        message_ts="100.1",
        text="<@U_ALICE> explain this error",
        mentions=["U_ALICE"],
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert len(ctx.invocations) == 4
    assert ctx.invocations[0][0] == "workflows/chat/should_react"
    assert ctx.invocations[1][0] == "workflows/chat/chat_thread_context"
    assert ctx.invocations[2][0] == "workflows/chat/chat_reply_intent"
    assert ctx.invocations[3][0] == "workflows/chat/chat_reply_actionable"
    assert ctx.invocations[3][1] == ()
    assert ctx.invocations[3][2] == {}
    assert "chat_reply_input" not in ctx.shared_state
    assert ctx.pipe == ""
    assert ctx.last_chat_reply_input is not None
    assert "thread_messages" in ctx.last_chat_reply_input
    assert ctx.last_chat_reply_input["previous_thread_context"]["thread_topic"] == ""
    assert ctx.last_chat_reply_input["thread_context"]["thread_topic"] == "thread topic"
    assert ctx.last_chat_reply_input["reply_intent"]["label"] == "answer"
    assert "message_ts" not in ctx.last_chat_reply_input["thread_messages"][0]
    assert set(ctx.last_chat_reply_input["thread_messages"][0].keys()) == {
        "content",
        "author",
        "author_type",
    }
    assert ctx.last_chat_reply_input["latest_message"]["author"] == "user_1"
    assert "@alice explain this error" in ctx.last_chat_reply_input["latest_message"]["content"]
    assert "explain this error" in ctx.last_pipe
    assert service.posts[0][1] == "確認します。まずエラーログを共有してください。"


@pytest.mark.asyncio
async def test_workflow_passes_transcript_and_reply_intent_to_actionable_reply_command(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="確認します。")
    _set_incoming_event(
        ctx,
        event_id="E_PIPE_RESTORE_1",
        message_ts="101.1",
        text="<@U_ALICE> explain this error",
        mentions=["U_ALICE"],
    )

    captured: dict[str, str] = {}

    async def invoke_capture_pipe(name: str, /, *args, **kwargs):
        ctx.invocations.append((name, args, kwargs))
        if name == "workflows/chat/should_react":
            return {"decision": "reply", "reason": "test", "reaction": None}
        if name == "workflows/chat/chat_thread_context":
            return {
                "thread_topic": "AI news this week",
                "latest_focus": "business angle grounded in this week's news, not generalities",
                "reason": "test",
                "confidence": 0.91,
            }
        if name == "workflows/chat/chat_reply_intent":
            return {"label": "supplement", "reason": "new detail", "confidence": 0.88}
        if name == "workflows/chat/chat_reply_actionable":
            captured["pipe_at_reply"] = ctx.pipe
            captured["previous_thread_context"] = ctx.shared_state["chat_reply_input"]["previous_thread_context"]
            captured["thread_context"] = ctx.shared_state["chat_reply_input"]["thread_context"]
            captured["reply_intent"] = ctx.shared_state["chat_reply_input"]["reply_intent"]
            return "確認します。"
        return "確認します。"

    ctx.invoke = invoke_capture_pipe

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert "pipe_at_reply" in captured
    assert "explain this error" in captured["pipe_at_reply"]
    assert captured["pipe_at_reply"].startswith("[user_1]")
    assert captured["previous_thread_context"]["thread_topic"] == ""
    assert captured["thread_context"]["latest_focus"] == "business angle grounded in this week's news, not generalities"
    assert captured["reply_intent"]["label"] == "supplement"


@pytest.mark.asyncio
async def test_workflow_formats_reply_with_talk_as_when_available(tmp_path, monkeypatch):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="ベース返信")
    _set_incoming_event(
        ctx,
        event_id="E_TALK_AS_1",
        message_ts="103.5",
        text="<@U_ALICE> persona reply",
        mentions=["U_ALICE"],
    )

    captured: dict[str, object] = {}

    async def fake_talk_as(context, topic, context_location, conversation_history):
        captured["topic"] = topic
        captured["context_location"] = context_location
        captured["conversation_history"] = conversation_history
        return "キャラ口調の返信です。"

    monkeypatch.setattr(chat_conversation_workflow, "talk_as", fake_talk_as)

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert service.posts[0][1] == "キャラ口調の返信です。"
    assert captured["topic"] == "ベース返信"
    assert captured["context_location"] == chat_conversation_workflow.t(
        "commands.workflows.chat_conversation_workflow.context_location"
    )
    history = captured["conversation_history"]
    assert isinstance(history, list)
    assert history
    assert history[-1].content == "@alice persona reply"


@pytest.mark.asyncio
async def test_workflow_preserves_speaker_identity_in_transcript_and_talk_as_history(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="ベース返信")
    thread_ts = "201.1"
    state_store.append_thread_message(
        "slack",
        "alice",
        "C1",
        thread_ts,
        ThreadMessageState(
            channel_id="C1",
            thread_ts=thread_ts,
            message_ts="201.2",
            author_id="U_OTHER_BOT",
            text="別エージェントの発話",
            mentions=[],
            is_bot_message=True,
        ),
    )
    state_store.append_thread_message(
        "slack",
        "alice",
        "C1",
        thread_ts,
        ThreadMessageState(
            channel_id="C1",
            thread_ts=thread_ts,
            message_ts="201.3",
            author_id="U_ALICE",
            text="自分の過去発話",
            mentions=[],
            is_bot_message=True,
        ),
    )
    _set_incoming_event(
        ctx,
        event_id="E_SPEAKER_1",
        message_ts="201.4",
        thread_ts=thread_ts,
        text="<@U_ALICE> 続けて",
        mentions=["U_ALICE"],
        is_thread_reply=True,
    )

    captured: dict[str, object] = {}

    async def fake_talk_as(context, topic, context_location, conversation_history):
        captured["conversation_history"] = conversation_history
        return "キャラ口調の返信です。"

    monkeypatch.setattr(chat_conversation_workflow, "talk_as", fake_talk_as)

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert "[agent_1] 別エージェントの発話" in ctx.last_pipe
    assert "[alice] 自分の過去発話" in ctx.last_pipe

    history = captured["conversation_history"]
    assert isinstance(history, list)
    assert any(
        isinstance(item, Message)
        and item.content == "別エージェントの発話"
        and item.author == "agent_1"
        and item.author_type == Message.USER
        for item in history
    )
    assert any(
        isinstance(item, Message)
        and item.content == "自分の過去発話"
        and item.author == "alice"
        and item.author_type == Message.ASSISTANT
        for item in history
    )
    assert any(
        isinstance(item, Message)
        and item.content == "@alice 続けて"
        and item.author == "user_1"
        for item in history
    )


@pytest.mark.asyncio
async def test_workflow_uses_runtime_identity_map_for_person_labels(tmp_path, monkeypatch):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    person = types.SimpleNamespace(
        person_id="alice",
        name="Alice",
        profile={"chat": {"subscriptions": [{"service": "slack", "channel_id": "C1", "enabled": True}]}},
    )
    ctx = RuntimeLookupContext(person=person, logger=StubLogger(), reply_text="ベース返信")
    ctx.team = types.SimpleNamespace(
        members=[
            person,
            types.SimpleNamespace(person_id="yuki", name="Yuki"),
        ]
    )
    ctx._identity_map = {"alice": "U_ALICE", "yuki": "U_OTHER_BOT"}
    thread_ts = "302.1"
    state_store.append_thread_message(
        "slack",
        "alice",
        "C1",
        thread_ts,
        ThreadMessageState(
            channel_id="C1",
            thread_ts=thread_ts,
            message_ts="302.2",
            author_id="U_OTHER_BOT",
            text="別エージェントの発話",
            mentions=[],
            is_bot_message=True,
        ),
    )
    _set_incoming_event(
        ctx,
        event_id="E_RUNTIME_LABEL_1",
        message_ts="302.3",
        thread_ts=thread_ts,
        text="<@U_ALICE> 続けて",
        mentions=["U_ALICE"],
        is_thread_reply=True,
    )

    captured: dict[str, object] = {}

    async def fake_talk_as(context, topic, context_location, conversation_history):
        captured["conversation_history"] = conversation_history
        return "キャラ口調の返信です。"

    monkeypatch.setattr(chat_conversation_workflow, "talk_as", fake_talk_as)

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert "[yuki] 別エージェントの発話" in ctx.last_pipe
    history = captured["conversation_history"]
    assert isinstance(history, list)
    assert any(
        isinstance(item, Message)
        and item.content == "別エージェントの発話"
        and item.author == "yuki"
        for item in history
    )


@pytest.mark.asyncio
async def test_workflow_does_not_reuse_runtime_person_labels_across_contexts(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    alice = types.SimpleNamespace(
        person_id="alice",
        name="Alice",
        profile={"chat": {"subscriptions": [{"service": "slack", "channel_id": "C1", "enabled": True}]}},
    )

    first_ctx = RuntimeLookupContext(person=alice, logger=StubLogger(), reply_text="ベース返信")
    first_ctx.team = types.SimpleNamespace(
        members=[
            alice,
            types.SimpleNamespace(person_id="yuki", name="Yuki"),
        ]
    )
    first_ctx._identity_map = {"alice": "U_ALICE", "yuki": "U_OTHER_BOT"}

    first_thread_ts = "303.1"
    state_store.append_thread_message(
        "slack",
        "alice",
        "C1",
        first_thread_ts,
        ThreadMessageState(
            channel_id="C1",
            thread_ts=first_thread_ts,
            message_ts="303.2",
            author_id="U_OTHER_BOT",
            text="別エージェントの発話",
            mentions=[],
            is_bot_message=True,
        ),
    )
    _set_incoming_event(
        first_ctx,
        event_id="E_RUNTIME_SCOPE_1",
        message_ts="303.3",
        thread_ts=first_thread_ts,
        text="<@U_ALICE> 続けて",
        mentions=["U_ALICE"],
        is_thread_reply=True,
    )

    async def fake_talk_as_first(context, topic, context_location, conversation_history):
        return "キャラ口調の返信です。"

    monkeypatch.setattr(chat_conversation_workflow, "talk_as", fake_talk_as_first)
    await chat_conversation_workflow.main(
        first_ctx, chat_service=service, state_store=state_store
    )

    second_ctx = _invoke_context_with_chat_profile(reply_text="ベース返信")
    second_thread_ts = "304.1"
    state_store.append_thread_message(
        "slack",
        "alice",
        "C1",
        second_thread_ts,
        ThreadMessageState(
            channel_id="C1",
            thread_ts=second_thread_ts,
            message_ts="304.2",
            author_id="U_OTHER_BOT",
            text="別エージェントの発話",
            mentions=[],
            is_bot_message=True,
        ),
    )
    _set_incoming_event(
        second_ctx,
        event_id="E_RUNTIME_SCOPE_2",
        message_ts="304.3",
        thread_ts=second_thread_ts,
        text="<@U_ALICE> 続けて",
        mentions=["U_ALICE"],
        is_thread_reply=True,
    )

    captured: dict[str, object] = {}

    async def fake_talk_as_second(context, topic, context_location, conversation_history):
        captured["conversation_history"] = conversation_history
        return "キャラ口調の返信です。"

    monkeypatch.setattr(chat_conversation_workflow, "talk_as", fake_talk_as_second)
    await chat_conversation_workflow.main(
        second_ctx, chat_service=service, state_store=state_store
    )

    history = captured["conversation_history"]
    assert isinstance(history, list)
    assert any(
        isinstance(item, Message)
        and item.content == "別エージェントの発話"
        and item.author == "agent_1"
        for item in history
    )


@pytest.mark.asyncio
async def test_workflow_always_uses_actionable_reply_command(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="今週のAIニュースを取得して要点を共有します。")
    _set_incoming_event(
        ctx,
        event_id="E_FRESH_1",
        message_ts="104.1",
        text="今週のAI関連ニュースでなにか面白いものあった?",
        mentions=["U_ALICE"],
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert len(ctx.invocations) == 4
    assert ctx.invocations[0][0] == "workflows/chat/should_react"
    assert ctx.invocations[1][0] == "workflows/chat/chat_thread_context"
    assert ctx.invocations[2][0] == "workflows/chat/chat_reply_intent"
    assert ctx.invocations[3][0] == "workflows/chat/chat_reply_actionable"
    assert service.posts[0][1] == "今週のAIニュースを取得して要点を共有します。"


@pytest.mark.asyncio
async def test_workflow_renders_participant_labels_back_to_service_mentions(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="@alice 了解しました。")
    _set_incoming_event(
        ctx,
        event_id="E_RENDER_1",
        message_ts="107.1",
        text="<@U_ALICE> ping",
        mentions=["U_ALICE"],
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert service.posts[0][1] == "<@U_ALICE> 了解しました。"


@pytest.mark.asyncio
async def test_workflow_ignores_edit_or_delete_events_and_marks_processed(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _context_with_chat_profile()
    _set_incoming_event(
        ctx,
        event_id="E_EDIT_1",
        message_ts="109.1",
        text="edited message",
        is_edit_or_delete=True,
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert service.posts == []
    assert service.reactions == []
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert "E_EDIT_1" in channel_state.processed_event_ids
    thread_messages = state_store.load_thread_messages("slack", "alice", "C1", "109.1")
    assert thread_messages == []


@pytest.mark.asyncio
async def test_workflow_passes_previous_thread_context_and_persists_updated_context(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="更新します。")
    thread_ts = "108.1"
    existing = state_store.load_thread_state("slack", "alice", "C1", thread_ts)
    existing.thread_topic = "weekly AI news"
    existing.latest_focus = "news-grounded business view"
    state_store.save_thread_state("slack", "alice", "C1", thread_ts, existing)

    captured: dict[str, object] = {}

    async def invoke(name: str, /, *args, **kwargs):
        ctx.invocations.append((name, args, kwargs))
        if name == "workflows/chat/should_react":
            return {"decision": "reply", "reason": "test", "reaction": None}
        if name == "workflows/chat/chat_thread_context":
            captured["previous_thread_context"] = ctx.shared_state["chat_reply_input"]["previous_thread_context"]
            return {
                "thread_topic": "weekly AI news",
                "latest_focus": "business examples from this week's news, not generalities",
                "reason": "updated",
                "confidence": 0.95,
            }
        if name == "workflows/chat/chat_reply_intent":
            return {"label": "clarify", "reason": "test", "confidence": 0.8}
        if name == "workflows/chat/chat_reply_actionable":
            return "更新します。"
        return "更新します。"

    ctx.invoke = invoke
    _set_incoming_event(
        ctx,
        event_id="E_CONTEXT_1",
        message_ts="108.2",
        thread_ts=thread_ts,
        text="今週のニュース前提で具体例を教えて",
        is_thread_reply=True,
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert captured["previous_thread_context"] == {
        "thread_topic": "weekly AI news",
        "latest_focus": "news-grounded business view",
    }
    updated = state_store.load_thread_state("slack", "alice", "C1", thread_ts)
    assert updated.thread_topic == "weekly AI news"
    assert updated.latest_focus == "business examples from this week's news, not generalities"


@pytest.mark.asyncio
async def test_workflow_does_not_clear_persisted_thread_context_when_classification_returns_empty(
    tmp_path,
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="更新します。")
    thread_ts = "108.9"
    existing = state_store.load_thread_state("slack", "alice", "C1", thread_ts)
    existing.thread_topic = "weekly AI news"
    existing.latest_focus = "news-grounded business view"
    state_store.save_thread_state("slack", "alice", "C1", thread_ts, existing)

    async def invoke(name: str, /, *args, **kwargs):
        ctx.invocations.append((name, args, kwargs))
        if name == "workflows/chat/should_react":
            return {"decision": "reply", "reason": "test", "reaction": None}
        if name == "workflows/chat/chat_thread_context":
            return {
                "thread_topic": "",
                "latest_focus": "",
                "reason": "classification_failed",
                "confidence": 0.0,
            }
        if name == "workflows/chat/chat_reply_intent":
            return {"label": "clarify", "reason": "test", "confidence": 0.8}
        if name == "workflows/chat/chat_reply_actionable":
            return "更新します。"
        return "更新します。"

    ctx.invoke = invoke
    _set_incoming_event(
        ctx,
        event_id="E_CONTEXT_EMPTY_1",
        message_ts="108.10",
        thread_ts=thread_ts,
        text="今週のニュース前提で具体例を教えて",
        is_thread_reply=True,
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    updated = state_store.load_thread_state("slack", "alice", "C1", thread_ts)
    assert updated.thread_topic == "weekly AI news"
    assert updated.latest_focus == "news-grounded business view"


@pytest.mark.asyncio
async def test_workflow_handles_incoming_event_from_shared_state(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _context_with_chat_profile()
    _set_incoming_event(
        ctx,
        event_id="E_SHARED_1",
        message_ts="103.1",
        text="<@U_ALICE> shared state path",
        mentions=["U_ALICE"],
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert len(service.posts) == 1
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert "E_SHARED_1" in channel_state.processed_event_ids


@pytest.mark.asyncio
async def test_workflow_ignores_non_trigger_and_marks_processed(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _context_with_chat_profile()
    _set_incoming_event(ctx, event_id="E2", message_ts="101.1", text="hello all", mentions=[])

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert service.posts == []
    assert service.reactions == []


@pytest.mark.asyncio
async def test_workflow_adds_reaction_when_should_react_returns_react_only(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="unused")
    _set_incoming_event(
        ctx,
        event_id="E_REACT_ONLY_1",
        message_ts="106.1",
        text="了解です",
        is_thread_reply=True,
    )

    async def invoke(name: str, /, *args, **kwargs):
        ctx.invocations.append((name, args, kwargs))
        if name == "workflows/chat/should_react":
            return {
                "decision": "react_only",
                "reason": "ack only",
                "reaction": "ack",
            }
        raise AssertionError(f"unexpected invocation: {name}")

    ctx.invoke = invoke

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert service.posts == []
    assert service.reactions == [("C1", "106.1", "ack")]
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert "E_REACT_ONLY_1" in channel_state.processed_event_ids
    thread_state = state_store.load_thread_state("slack", "alice", "C1", "106.1")
    assert "alice" in thread_state.participants


@pytest.mark.asyncio
async def test_workflow_resolves_channel_name_when_channel_id_missing(tmp_path):
    service = FakeChatService()
    service.channel_name_map["dev-chat"] = "C1"
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _context_with_channel_name_subscription()
    _set_incoming_event(
        ctx,
        event_id="E3",
        channel_id="C1",
        message_ts="102.1",
        text="<@U_ALICE> ping",
        mentions=["U_ALICE"],
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert len(service.posts) == 1
    assert service.posts[0][0] == "C1"


@pytest.mark.asyncio
async def test_workflow_does_not_duplicate_reply_after_crash_before_mark_processed(tmp_path):
    service = FakeChatService()
    state_store = CrashAfterPostStateStore(base_dir=tmp_path)
    ctx = _context_with_chat_profile()
    _set_incoming_event(
        ctx,
        event_id="E4",
        message_ts="103.1",
        text="<@U_ALICE> retry-safe?",
        mentions=["U_ALICE"],
    )

    with pytest.raises(RuntimeError, match="simulated crash after post"):
        await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)
    assert len(service.posts) == 1
    # Side effect success is recorded before the simulated crash.
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert "E4" in channel_state.processed_event_ids

    # Retry should not create a duplicate reply for the same event.
    _set_incoming_event(
        ctx,
        event_id="E4",
        message_ts="103.1",
        text="<@U_ALICE> retry-safe?",
        mentions=["U_ALICE"],
    )
    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)
    assert len(service.posts) == 1
