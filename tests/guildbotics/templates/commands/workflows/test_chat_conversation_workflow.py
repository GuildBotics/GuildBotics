from __future__ import annotations

import types

import pytest

from guildbotics.integrations.chat_service import (
    ChatEvent,
    ChatIdentity,
    ChatPostResult,
)
from guildbotics.integrations.chat_state_store import ThreadMessageState
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.runtime.event_listener import (
    INCOMING_CHAT_EVENT_KEY,
    IncomingChatEvent,
)
from guildbotics.templates.commands.workflows import chat_conversation_workflow
from guildbotics.utils.memory_backend import (
    MemoryContext,
    MemoryForgetResult,
    MemoryItem,
    MemoryUpdate,
    MemoryWriteResult,
)

EXPECTED_CHAT_INVOCATIONS = 4


@pytest.fixture(autouse=True)
def _disable_live_memory_backend(monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_MEMORY_BACKEND", "none")


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
        self._memory_update = {
            "should_update": False,
            "topic_id": "",
            "title": "",
            "summary": "",
            "memory": "",
            "reason": "default",
            "confidence": 0.0,
        }
        self.invocations: list[tuple[str, tuple, dict]] = []
        self.last_chat_reply_input = None
        self.last_chat_memory_update_input = None
        self.last_pipe = ""

    async def invoke(self, name: str, /, *args, **kwargs):
        self.invocations.append((name, args, kwargs))
        if "chat_reply_input" in self.shared_state:
            self.last_chat_reply_input = self.shared_state["chat_reply_input"]
        if "chat_memory_update_input" in self.shared_state:
            self.last_chat_memory_update_input = self.shared_state[
                "chat_memory_update_input"
            ]
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
        if name == "workflows/chat/chat_memory_update":
            return self._memory_update
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


class FakeMemoryRepository:
    def __init__(self, path):
        self.path = path
        self.commit_messages: list[str] = []

    def get_repo_path(self):
        return self.path

    def commit_if_changed(self, message: str):
        self.commit_messages.append(message)
        return "deadbeef"


class FakeMemoryBackend:
    def __init__(self, path):
        self.path = path
        self.updates = []
        self.forgets = []
        self.queries = []
        self.items = []

    def recall(self, query):
        self.queries.append(query)
        return MemoryContext(
            backend="fake",
            person_id=query.person_id,
            query=query.trace_payload(),
            items=self.items,
        )

    def remember(self, update):
        self.updates.append(update)
        return MemoryWriteResult(
            changed=update.should_update,
            backend="fake",
            reference="fake-ref",
            person_id=str(update.scope.get("person_id", "")),
            item_id=update.topic_id,
            title=update.title,
            source=update.source,
            scope=update.scope,
            metadata=update.metadata,
            retention=update.retention,
        )

    def forget(self, request):
        self.forgets.append(request)
        return MemoryForgetResult(
            changed=True,
            backend="fake",
            reference="fake-ref",
            person_id=request.person_id,
            item_id=request.item_id,
            source=request.source,
            scope=request.scope,
            metadata=request.metadata,
        )


class FailingRecallMemoryBackend(FakeMemoryBackend):
    def recall(self, query):
        raise RuntimeError("recall failed")


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
async def test_workflow_uses_chat_reply_command_when_invoker_is_available(
    tmp_path, monkeypatch
):
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
    monkeypatch.setattr(
        chat_conversation_workflow,
        "get_workspace_path",
        lambda person_id: tmp_path / "workspace" / person_id,
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert len(ctx.invocations) == EXPECTED_CHAT_INVOCATIONS
    assert ctx.invocations[0][0] == "workflows/chat/should_react"
    assert ctx.invocations[1][0] == "workflows/chat/chat_thread_context"
    assert ctx.invocations[2][0] == "workflows/chat/chat_reply_intent"
    assert ctx.invocations[3][0] == "workflows/chat/chat_reply_actionable"
    assert ctx.invocations[3][1] == ()
    assert ctx.invocations[3][2] == {"cwd": tmp_path / "workspace" / "alice"}
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
    assert ctx.last_chat_reply_input["agent_profile"]["person_id"] == "alice"
    assert ctx.last_chat_reply_input["latest_message"]["author"] == "user_1"
    assert "@alice explain this error" in ctx.last_chat_reply_input["latest_message"]["content"]
    assert "explain this error" in ctx.last_pipe
    assert service.posts[0][1] == "確認します。まずエラーログを共有してください。"


@pytest.mark.asyncio
async def test_workflow_exposes_normalized_memory_context_in_reply_input(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="確認します。")
    memory_backend = FakeMemoryBackend(tmp_path / "memory")
    memory_backend.items = [
        MemoryItem(
            id="focusflow-onboarding-plan",
            title="FocusFlow Onboarding Plan",
            summary="FocusFlow onboarding decisions",
            path="topics/focusflow-onboarding-plan/memory.md",
            content="# FocusFlow Onboarding Plan",
            score=1.0,
            match_reason="Matched fake memory title.",
            source={
                "type": "slack_thread",
                "service": "slack",
                "channel": "C1",
                "thread_ts": "100.1",
            },
            scope={"person_id": "alice"},
        )
    ]
    _set_incoming_event(
        ctx,
        event_id="E_MEMORY_CONTEXT_1",
        message_ts="100.1",
        text="<@U_ALICE> FocusFlow onboarding?",
        mentions=["U_ALICE"],
    )
    monkeypatch.setattr(
        chat_conversation_workflow,
        "_get_memory_backend",
        lambda context: memory_backend,
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert ctx.last_chat_reply_input is not None
    memory_context = ctx.last_chat_reply_input["memory_context"]
    assert memory_context["backend"] == "fake"
    assert memory_context["person_id"] == "alice"
    assert memory_context["query"]["source"]["thread_ts"] == "100.1"
    assert memory_context["items"][0]["id"] == "focusflow-onboarding-plan"
    assert memory_context["items"][0]["match_reason"] == "Matched fake memory title."


@pytest.mark.asyncio
async def test_workflow_continues_reply_when_memory_recall_fails(tmp_path, monkeypatch):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="確認します。")
    memory_backend = FailingRecallMemoryBackend(tmp_path / "memory")
    _set_incoming_event(
        ctx,
        event_id="E_MEMORY_RECALL_FAIL_1",
        message_ts="100.2",
        text="<@U_ALICE> FocusFlow onboarding?",
        mentions=["U_ALICE"],
    )
    monkeypatch.setattr(
        chat_conversation_workflow,
        "_get_memory_backend",
        lambda context: memory_backend,
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert service.posts[0][1] == "確認します。"
    assert ctx.last_chat_reply_input is not None
    memory_context = ctx.last_chat_reply_input["memory_context"]
    assert memory_context["backend"] == "failingrecall"
    assert memory_context["status"] == "failed"
    assert memory_context["error"] == {
        "type": "RuntimeError",
        "message": "recall failed",
    }
    assert memory_context["items"] == []
    assert any("memory recall failed" in line[0] for line in ctx.logger.lines)


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
async def test_workflow_posts_actionable_reply_and_updates_memory_backend(tmp_path, monkeypatch):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="ベース返信")
    memory_backend = FakeMemoryBackend(tmp_path / "memory")
    ctx._memory_update = {
        "should_update": True,
        "topic_id": "onboarding",
        "title": "Onboarding",
        "summary": "Initial onboarding decisions",
        "memory": "# Onboarding\n\n## Decisions\n- Keep the first step short.",
        "retention": {
            "status": "temporary",
            "expires_at": "2026-05-01T00:00:00+09:00",
            "reason": "Demo-only handling.",
        },
        "reason": "durable decision",
        "confidence": 0.9,
    }
    _set_incoming_event(
        ctx,
        event_id="E_TALK_AS_1",
        message_ts="1777554000.000000",
        text="<@U_ALICE> persona reply",
        mentions=["U_ALICE"],
    )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "_get_memory_backend",
        lambda context: memory_backend,
    )
    monkeypatch.setattr(
        chat_conversation_workflow,
        "get_workspace_path",
        lambda person_id: tmp_path / "workspace" / person_id,
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert service.posts[0][1] == "ベース返信"
    assert ctx.invocations[3][2] == {"cwd": tmp_path / "workspace" / "alice"}
    assert ctx.invocations[4][0] == "workflows/chat/chat_memory_update"
    assert memory_backend.updates[0].topic_id == "onboarding"
    assert memory_backend.updates[0].source == {
            "type": "slack_thread",
            "service": "slack",
            "channel": "C1",
            "thread_ts": "1777554000.000000",
        }
    assert memory_backend.updates[0].scope == {"person_id": "alice"}
    assert memory_backend.updates[0].retention["status"] == "temporary"
    assert memory_backend.updates[0].retention["kind"] == "temporary"
    assert (
        memory_backend.updates[0].retention["expires_at"]
        == "2026-05-01T00:00:00+09:00"
    )
    assert memory_backend.updates[0].retention["reason"] == "Demo-only handling."
    assert memory_backend.updates[0].retention["effective_at"]
    assert ctx.last_chat_memory_update_input is not None
    assert ctx.last_chat_memory_update_input["reply_text"] == "ベース返信"
    assert ctx.last_chat_memory_update_input["event_time"]["message_ts"] == (
        "1777554000.000000"
    )
    assert "iso" in ctx.last_chat_memory_update_input["event_time"]
    assert "iso" in ctx.last_chat_memory_update_input["current_time"]
    assert [
        message["content"]
        for message in ctx.last_chat_memory_update_input["thread_messages"]
    ] == ["@alice persona reply"]


@pytest.mark.asyncio
async def test_workflow_records_transition_memory_before_replacement(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="更新します。")
    memory_backend = FakeMemoryBackend(tmp_path / "memory")
    memory_backend.items = [
        MemoryItem(
            id="focusflow-onboarding-plan",
            title="FocusFlow Onboarding Plan",
            summary="Old onboarding plan",
            path="cognee://guildbotics:person:alice/focusflow-onboarding-plan",
            content="# FocusFlow Onboarding Plan\n\n## Decisions\n- 通知初期値は弱め。",
            score=1.0,
            match_reason="Matched fake backend memory.",
            scope={"person_id": "alice"},
        )
    ]
    ctx._memory_update = {
        "should_update": True,
        "topic_id": "focusflow-onboarding-plan",
        "title": "FocusFlow Onboarding Plan",
        "summary": "Updated onboarding plan",
        "memory": "# FocusFlow Onboarding Plan\n\n## Decisions\n- 通知初期値はオフ。",
        "reason": "User replaced the previous notification default.",
        "confidence": 0.9,
    }
    _set_incoming_event(
        ctx,
        event_id="E_MEMORY_REPLACE_1",
        message_ts="103.6",
        text="<@U_ALICE> 通知初期値は弱めではなくオフにしてください",
        mentions=["U_ALICE"],
    )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "_get_memory_backend",
        lambda context: memory_backend,
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert memory_backend.forgets == []
    transition_update, current_update = memory_backend.updates
    assert transition_update.topic_id.startswith(
        "focusflow-onboarding-plan-transition-"
    )
    assert transition_update.retention["kind"] == "transition"
    assert transition_update.retention["subject_item_id"] == (
        "focusflow-onboarding-plan"
    )
    assert transition_update.retention["effective_at"]
    assert "通知初期値は弱め" in transition_update.memory
    assert "通知初期値はオフ" in transition_update.memory
    assert current_update.topic_id == "focusflow-onboarding-plan"
    assert current_update.retention["kind"] == "current_fact"
    assert current_update.retention["effective_at"]


@pytest.mark.asyncio
async def test_workflow_coalesces_similar_topic_update_to_existing_item(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="更新します。")
    memory_backend = FakeMemoryBackend(tmp_path / "memory")
    memory_backend.items = [
        MemoryItem(
            id="focusflow-onboarding-policy",
            title="FocusFlow onboarding policy",
            summary="Current onboarding policy",
            path="cognee://guildbotics:person:alice/focusflow-onboarding-policy",
            content="# FocusFlow onboarding policy\n\n## Decisions\n- 通知初期値は弱め。",
            score=1.0,
            match_reason="Matched fake backend memory.",
            scope={"person_id": "alice"},
        )
    ]
    ctx._memory_update = {
        "should_update": True,
        "topic_id": "focusflow-onboarding-post-completion-today-focus-plan",
        "title": "FocusFlow onboarding post-completion: today focus plan",
        "summary": "Post completion routing decision",
        "memory": "# FocusFlow onboarding post-completion: today focus plan\n\n## Decisions\n- 完了後は今日の集中プランへ。",
        "reason": "User fixed post-completion destination.",
        "confidence": 0.9,
    }
    _set_incoming_event(
        ctx,
        event_id="E_MEMORY_COALESCE_1",
        message_ts="103.65",
        text="<@U_ALICE> FocusFlow onboarding の完了後遷移先を今日の集中プランに固定してください",
        mentions=["U_ALICE"],
    )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "_get_memory_backend",
        lambda context: memory_backend,
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    transition_update, current_update = memory_backend.updates
    assert transition_update.retention["kind"] == "transition"
    assert transition_update.retention["subject_item_id"] == "focusflow-onboarding-policy"
    assert current_update.topic_id == "focusflow-onboarding-policy"


def test_with_default_memory_retention_downgrades_invalid_transition_kind():
    update = MemoryUpdate(
        should_update=True,
        topic_id="focusflow-onboarding-policy",
        title="FocusFlow onboarding policy",
        summary="summary",
        memory="# FocusFlow onboarding policy",
        retention={
            "status": "active",
            "kind": "transition",
            "subject_item_id": "",
            "effective_at": "",
        },
    )
    normalized = chat_conversation_workflow._with_default_memory_retention(
        update, event=None
    )
    assert normalized.retention["kind"] == "current_fact"


def test_coalesce_memory_topic_rewrites_transition_topic_to_subject():
    update = MemoryUpdate(
        should_update=True,
        topic_id="focusflow-onboarding-policy-transition-1",
        title="FocusFlow onboarding policy Change",
        summary="summary",
        memory="# memory",
    )
    memory_context = MemoryContext(
        backend="fake",
        person_id="alice",
        query={},
        items=[
            MemoryItem(
                id="focusflow-onboarding-policy",
                title="FocusFlow onboarding policy",
                summary="current",
                path="fake://current",
                content="# current",
                score=1.0,
                match_reason="",
                retention={"kind": "current_fact", "status": "active"},
            ),
            MemoryItem(
                id="focusflow-onboarding-policy-transition-1",
                title="FocusFlow onboarding policy Change",
                summary="transition",
                path="fake://transition",
                content="# transition",
                score=1.0,
                match_reason="",
                retention={
                    "kind": "transition",
                    "status": "active",
                    "subject_item_id": "focusflow-onboarding-policy",
                },
            ),
        ],
    )

    normalized = chat_conversation_workflow._coalesce_memory_topic(
        update, memory_context
    )
    assert normalized.topic_id == "focusflow-onboarding-policy"
    assert normalized.title == "FocusFlow onboarding policy"


def test_coalesce_memory_topic_single_transition_candidate_uses_subject_id():
    update = MemoryUpdate(
        should_update=True,
        topic_id="focusflow-onboarding-post-completion-today-focus-plan",
        title="FocusFlow onboarding post-completion: today focus plan",
        summary="summary",
        memory="# memory",
    )
    memory_context = MemoryContext(
        backend="fake",
        person_id="alice",
        query={},
        items=[
            MemoryItem(
                id="focusflow-onboarding-policy-transition-1",
                title="FocusFlow onboarding policy Change",
                summary="transition",
                path="fake://transition",
                content="# transition",
                score=1.0,
                match_reason="",
                retention={
                    "kind": "transition",
                    "status": "active",
                    "subject_item_id": "focusflow-onboarding-policy",
                },
            ),
        ],
    )

    normalized = chat_conversation_workflow._coalesce_memory_topic(
        update, memory_context
    )
    assert normalized.topic_id == "focusflow-onboarding-policy"
    assert normalized.title == "FocusFlow onboarding post-completion: today focus plan"


@pytest.mark.asyncio
async def test_workflow_records_transition_when_only_transition_context_matches_subject(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="更新します。")
    memory_backend = FakeMemoryBackend(tmp_path / "memory")
    memory_backend.items = [
        MemoryItem(
            id="focusflow-onboarding-policy-transition-1",
            title="FocusFlow onboarding policy Change",
            summary="Notification default changed from weak to off",
            path="cognee://guildbotics:person:alice/focusflow-onboarding-policy-transition-1",
            content="# FocusFlow onboarding policy Change\n\n## Previous Memory Excerpt\n- 通知初期値は弱め。",
            score=1.0,
            match_reason="Matched fake backend memory.",
            scope={"person_id": "alice"},
            retention={
                "kind": "transition",
                "status": "active",
                "subject_item_id": "focusflow-onboarding-policy",
            },
        )
    ]
    ctx._memory_update = {
        "should_update": True,
        "topic_id": "focusflow-onboarding-post-completion-today-focus-plan",
        "title": "FocusFlow onboarding post-completion: today focus plan",
        "summary": "Post completion routing decision",
        "memory": "# FocusFlow onboarding post-completion: today focus plan\n\n## Decisions\n- 完了後は今日の集中プランへ。",
        "reason": "User fixed post-completion destination.",
        "confidence": 0.9,
    }
    _set_incoming_event(
        ctx,
        event_id="E_MEMORY_COALESCE_2",
        message_ts="103.66",
        text="<@U_ALICE> FocusFlow onboarding の完了後遷移先を今日の集中プランに固定してください",
        mentions=["U_ALICE"],
    )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "_get_memory_backend",
        lambda context: memory_backend,
    )

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    transition_update, current_update = memory_backend.updates
    assert transition_update.retention["kind"] == "transition"
    assert transition_update.retention["subject_item_id"] == "focusflow-onboarding-policy"
    assert current_update.topic_id == "focusflow-onboarding-policy"


@pytest.mark.asyncio
async def test_workflow_forgets_memory_without_replacement(tmp_path, monkeypatch):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="忘れます。")
    memory_backend = FakeMemoryBackend(tmp_path / "memory")
    memory_backend.items = [
        MemoryItem(
            id="demo-cta",
            title="Demo CTA",
            summary="Temporary demo CTA",
            path="cognee://guildbotics:person:alice/demo-cta",
            content="# Demo CTA",
            score=1.0,
            match_reason="Matched fake backend memory.",
            scope={"person_id": "alice"},
        )
    ]
    ctx._memory_update = {
        "should_update": False,
        "topic_id": "",
        "title": "",
        "summary": "",
        "memory": "",
        "forget_item_ids": ["demo-cta"],
        "forget_reason": "User explicitly asked not to use the demo CTA anymore.",
        "reason": "forget only",
        "confidence": 0.9,
    }
    _set_incoming_event(
        ctx,
        event_id="E_MEMORY_FORGET_1",
        message_ts="103.7",
        text="<@U_ALICE> デモ用CTAの話は忘れてください",
        mentions=["U_ALICE"],
    )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "_get_memory_backend",
        lambda context: memory_backend,
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert [request.item_id for request in memory_backend.forgets] == ["demo-cta"]
    assert memory_backend.forgets[0].reason == (
        "User explicitly asked not to use the demo CTA anymore."
    )
    assert memory_backend.updates[0].should_update is False


@pytest.mark.asyncio
async def test_memory_update_gate_suppresses_unconfirmed_agent_suggestion(monkeypatch):
    ctx = _invoke_context_with_chat_profile(reply_text="映画候補を出します")
    captured = {}
    suppression_confidence = 0.91

    async def suppress_update(context, payload):
        captured["payload"] = payload
        return types.SimpleNamespace(
            label="suppress",
            status="unadopted_possibility",
            reason="Agent recommendation was not confirmed by the user.",
            confidence=suppression_confidence,
        )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "should_keep_chat_memory_update",
        suppress_update,
    )

    update = await chat_conversation_workflow._gate_memory_update(
        ctx,
        update=MemoryUpdate(
            should_update=True,
            topic_id="movie-recommendations",
            title="Movie Recommendations",
            summary="Movie recommendations",
            memory="# Movie Recommendations\n\n## Current Direction\n- ...",
            metadata={"reason": "Stores recommendations; no decision has been made."},
        ),
        proposal={
            "should_update": True,
            "topic_id": "movie-recommendations",
            "reason": "Stores recommendations; no decision has been made.",
        },
        payload={
            "agent_profile": {"name": "Alice", "roles": []},
            "thread_context": {"thread_topic": "映画のおすすめ"},
            "memory_context": {"items": []},
            "thread_messages": [{"author": "user_1", "content": "おすすめの映画は?"}],
            "reply_text": "まず1本なら...",
        },
    )

    assert captured["payload"]["proposal"]["topic_id"] == "movie-recommendations"
    assert captured["payload"]["agent_profile"] == {"name": "Alice", "roles": []}
    assert update.should_update is False
    assert update.metadata["suppressed_reason"] == (
        "Agent recommendation was not confirmed by the user."
    )
    assert update.metadata["retention_status"] == "unadopted_possibility"
    assert update.metadata["suppression_confidence"] == suppression_confidence


@pytest.mark.asyncio
async def test_memory_update_gate_suppresses_keep_without_evidence(monkeypatch):
    ctx = _invoke_context_with_chat_profile(reply_text="イベント候補を出します")

    async def keep_without_evidence(context, payload):
        return types.SimpleNamespace(
            label="keep",
            status="open_loop",
            reason="Looks useful for future planning.",
            evidence=[],
            evidence_support="none",
            confidence=0.78,
        )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "should_keep_chat_memory_update",
        keep_without_evidence,
    )

    update = await chat_conversation_workflow._gate_memory_update(
        ctx,
        update=MemoryUpdate(
            should_update=True,
            topic_id="gw-yamaguchi-2026",
            title="GW events around Yamaguchi 2026",
            summary="GW event options",
            memory="# GW events around Yamaguchi 2026",
            metadata={"reason": "Stores event options."},
        ),
        proposal={"topic_id": "gw-yamaguchi-2026"},
        payload={
            "agent_profile": {"name": "Alice"},
            "thread_context": {"thread_topic": "GW events"},
            "memory_context": {"items": []},
            "thread_messages": [
                {
                    "author": "user_1",
                    "content": "このGWにはなにかおすすめのイベントあります?",
                }
            ],
            "reply_text": "候補は...",
        },
    )

    assert update.should_update is False
    assert update.metadata["suppressed_reason"] == (
        "memory retention gate returned keep without thread evidence"
    )
    assert update.metadata["retention_status"] == "open_loop"


@pytest.mark.asyncio
async def test_memory_update_gate_suppresses_topic_only_evidence(monkeypatch):
    ctx = _invoke_context_with_chat_profile(reply_text="Step 2の方針を提案します")

    async def keep_with_topic_only_evidence(context, payload):
        return types.SimpleNamespace(
            label="keep",
            status="open_loop",
            reason="The topic may affect future implementation.",
            evidence=[
                "Step 2の「最初に登録する3つのタスク」は必須にするべきですか?"
            ],
            evidence_support="topic_only",
            confidence=0.9,
        )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "should_keep_chat_memory_update",
        keep_with_topic_only_evidence,
    )

    update = await chat_conversation_workflow._gate_memory_update(
        ctx,
        update=MemoryUpdate(
            should_update=True,
            topic_id="focusflow-onboarding-initial-tasks",
            title="FocusFlow onboarding: initial 3 tasks",
            summary="Proposed direction for Step 2",
            memory="# FocusFlow onboarding: initial 3 tasks\n\n## Current Direction\n- 1 required, 2 recommended.",
            metadata={"reason": "Stores proposed direction."},
        ),
        proposal={"topic_id": "focusflow-onboarding-initial-tasks"},
        payload={
            "agent_profile": {"name": "Alice"},
            "thread_context": {"thread_topic": "FocusFlow Step 2"},
            "memory_context": {"items": []},
            "thread_messages": [
                {
                    "author": "user_1",
                    "content": "Step 2の「最初に登録する3つのタスク」は必須にするべきですか?",
                }
            ],
            "reply_text": "3つとも必須にはしないほうがいいと思う。",
        },
    )

    assert update.should_update is False
    assert update.metadata["suppressed_reason"] == (
        "memory retention evidence does not support the proposed memory content"
    )
    assert update.metadata["retention_status"] == "open_loop"


@pytest.mark.asyncio
async def test_memory_update_gate_suppresses_open_loop_with_empty_open_questions(
    monkeypatch,
):
    ctx = _invoke_context_with_chat_profile(reply_text="整理します")

    async def keep_open_loop(context, payload):
        return types.SimpleNamespace(
            label="keep",
            status="open_loop",
            reason="There are unresolved items to revisit.",
            evidence=["3つのタスク登録を必須にするかは未決です。"],
            evidence_support="supports_memory",
            confidence=0.84,
        )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "should_keep_chat_memory_update",
        keep_open_loop,
    )

    update = await chat_conversation_workflow._gate_memory_update(
        ctx,
        update=MemoryUpdate(
            should_update=True,
            topic_id="focusflow-onboarding-policy",
            title="FocusFlow onboarding policy",
            summary="Updated onboarding policy",
            memory=(
                "# FocusFlow onboarding policy\n\n"
                "## Decisions\n- 通知の初期値は弱め。\n\n"
                "## Open Questions\n- None\n\n"
                "## Current Direction\n- 継続検討。"
            ),
            metadata={"reason": "test"},
        ),
        proposal={"topic_id": "focusflow-onboarding-policy"},
        payload={
            "agent_profile": {"name": "Alice"},
            "thread_context": {"thread_topic": "FocusFlow"},
            "memory_context": {"items": []},
            "thread_messages": [
                {
                    "author": "user_1",
                    "content": "3つのタスク登録を必須にするかは未決です。",
                }
            ],
            "reply_text": "整理します",
        },
    )

    assert update.should_update is False
    assert update.metadata["suppressed_reason"] == (
        "memory retention status open_loop conflicts with empty Open Questions"
    )
    assert update.metadata["retention_status"] == "open_loop"


@pytest.mark.asyncio
async def test_memory_update_gate_suppresses_open_loop_when_open_questions_section_missing(
    monkeypatch,
):
    ctx = _invoke_context_with_chat_profile(reply_text="整理します")

    async def keep_open_loop(context, payload):
        return types.SimpleNamespace(
            label="keep",
            status="open_loop",
            reason="There are unresolved items to revisit.",
            evidence=["3つのタスク登録を必須にするかは未決です。"],
            evidence_support="supports_memory",
            confidence=0.84,
        )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "should_keep_chat_memory_update",
        keep_open_loop,
    )

    update = await chat_conversation_workflow._gate_memory_update(
        ctx,
        update=MemoryUpdate(
            should_update=True,
            topic_id="focusflow-onboarding-policy",
            title="FocusFlow onboarding policy",
            summary="Updated onboarding policy",
            memory=(
                "# FocusFlow onboarding policy\n\n"
                "## Decisions\n- 通知の初期値は弱め。\n\n"
                "## Current Direction\n- 継続検討。"
            ),
            metadata={"reason": "test"},
        ),
        proposal={"topic_id": "focusflow-onboarding-policy"},
        payload={
            "agent_profile": {"name": "Alice"},
            "thread_context": {"thread_topic": "FocusFlow"},
            "memory_context": {"items": []},
            "thread_messages": [
                {
                    "author": "user_1",
                    "content": "3つのタスク登録を必須にするかは未決です。",
                }
            ],
            "reply_text": "整理します",
        },
    )

    assert update.should_update is False
    assert update.metadata["suppressed_reason"] == (
        "memory retention status open_loop conflicts with empty Open Questions"
    )
    assert update.metadata["retention_status"] == "open_loop"


@pytest.mark.asyncio
async def test_memory_update_gate_applies_temporary_retention(monkeypatch):
    ctx = _invoke_context_with_chat_profile(reply_text="了解です")

    async def keep_temporary(context, payload):
        return types.SimpleNamespace(
            label="keep",
            status="future_relevance",
            reason="Demo-only, time-limited direction.",
            evidence=["今日の社内デモだけ、CTAを少し派手に見せたいです。"],
            evidence_support="supports_memory",
            retention_mode="temporary",
            temporary_expires_at="2026-05-30T00:00:00+09:00",
            confidence=0.91,
        )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "should_keep_chat_memory_update",
        keep_temporary,
    )

    update = await chat_conversation_workflow._gate_memory_update(
        ctx,
        update=MemoryUpdate(
            should_update=True,
            topic_id="focusflow-cta",
            title="FocusFlow CTA",
            summary="CTA policy",
            memory="# FocusFlow CTA",
            metadata={"reason": "test"},
        ),
        proposal={"topic_id": "focusflow-cta"},
        payload={
            "agent_profile": {"name": "Alice"},
            "thread_context": {"thread_topic": "FocusFlow CTA"},
            "event_time": {"iso": "2026-05-29T19:04:40+09:00"},
            "current_time": {"iso": "2026-05-29T19:04:41+09:00"},
            "memory_context": {"items": []},
            "thread_messages": [
                {
                    "author": "user_1",
                    "content": "今日の社内デモだけ、CTAを少し派手に見せたいです。",
                }
            ],
            "reply_text": "了解です",
        },
    )

    assert update.should_update is True
    assert update.retention["status"] == "temporary"
    assert update.retention["kind"] == "temporary"
    assert update.retention["expires_at"] == "2026-05-30T00:00:00+09:00"


@pytest.mark.asyncio
async def test_memory_update_gate_suppresses_temporary_without_absolute_expires(
    monkeypatch,
):
    ctx = _invoke_context_with_chat_profile(reply_text="了解です")

    async def keep_temporary_without_expires(context, payload):
        return types.SimpleNamespace(
            label="keep",
            status="future_relevance",
            reason="Demo-only, but no valid expiry provided.",
            evidence=["今日の社内デモだけ、CTAを少し派手に見せたいです。"],
            evidence_support="supports_memory",
            retention_mode="temporary",
            temporary_expires_at="",
            confidence=0.91,
        )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "should_keep_chat_memory_update",
        keep_temporary_without_expires,
    )

    update = await chat_conversation_workflow._gate_memory_update(
        ctx,
        update=MemoryUpdate(
            should_update=True,
            topic_id="focusflow-cta",
            title="FocusFlow CTA",
            summary="CTA policy",
            memory="# FocusFlow CTA",
            metadata={"reason": "test"},
        ),
        proposal={"topic_id": "focusflow-cta"},
        payload={
            "agent_profile": {"name": "Alice"},
            "thread_context": {"thread_topic": "FocusFlow CTA"},
            "event_time": {"iso": "2026-05-29T19:04:40+09:00"},
            "current_time": {"iso": "2026-05-29T19:04:41+09:00"},
            "memory_context": {"items": []},
            "thread_messages": [
                {
                    "author": "user_1",
                    "content": "今日の社内デモだけ、CTAを少し派手に見せたいです。",
                }
            ],
            "reply_text": "了解です",
        },
    )

    assert update.should_update is False
    assert update.metadata["suppressed_reason"] == (
        "temporary retention decision requires absolute expires_at"
    )


@pytest.mark.asyncio
async def test_memory_update_gate_promotes_temporary_when_proposal_should_update_false(
    monkeypatch,
):
    ctx = _invoke_context_with_chat_profile(reply_text="了解です")

    async def keep_temporary(context, payload):
        return types.SimpleNamespace(
            label="keep",
            status="future_relevance",
            reason="Demo-only temporary context.",
            evidence=["今日の社内デモだけ、CTAを少し派手に見せたいです。"],
            evidence_support="supports_memory",
            retention_mode="temporary",
            temporary_expires_at="2026-05-30T00:00:00+09:00",
            confidence=0.9,
        )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "should_keep_chat_memory_update",
        keep_temporary,
    )

    update = await chat_conversation_workflow._gate_memory_update(
        ctx,
        update=MemoryUpdate(
            should_update=False,
            topic_id="focusflow-cta",
            title="FocusFlow CTA",
            summary="",
            memory="",
            metadata={},
        ),
        proposal={"topic_id": "focusflow-cta"},
        payload={
            "agent_profile": {"name": "Alice"},
            "thread_context": {
                "thread_topic": "FocusFlow CTA",
                "latest_focus": "本日社内デモのみCTAを少し派手に見せる一時対応を行うこと。",
            },
            "event_time": {"iso": "2026-05-29T20:13:54+09:00"},
            "current_time": {"iso": "2026-05-29T20:13:55+09:00"},
            "memory_context": {"items": []},
            "thread_messages": [
                {
                    "author": "user_1",
                    "content": "今日の社内デモだけ、CTAを少し派手に見せたいです。",
                }
            ],
            "reply_text": "了解です",
        },
    )

    assert update.should_update is True
    assert update.retention["status"] == "temporary"
    assert update.retention["kind"] == "temporary"
    assert update.retention["expires_at"] == "2026-05-30T00:00:00+09:00"
    assert update.metadata["temporary_promoted_from_should_update_false"] is True
    assert update.memory.strip().startswith("# FocusFlow CTA")


@pytest.mark.asyncio
async def test_workflow_temporary_update_does_not_replace_current_or_create_transition(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="了解です")
    memory_backend = FakeMemoryBackend(tmp_path / "memory")
    memory_backend.items = [
        MemoryItem(
            id="focusflow-cta",
            title="FocusFlow CTA",
            summary="Current CTA policy",
            path="cognee://guildbotics:person:alice/focusflow-cta",
            content="# FocusFlow CTA\n\n## Decisions\n- CTAは「今日の集中プランを作る」",
            score=1.0,
            match_reason="Matched fake backend memory.",
            scope={"person_id": "alice"},
            retention={"status": "active", "kind": "current_fact"},
        )
    ]
    ctx._memory_update = {
        "should_update": True,
        "topic_id": "focusflow-cta",
        "title": "FocusFlow CTA",
        "summary": "Demo-only CTA styling",
        "memory": "# FocusFlow CTA\n\n## Current Direction\n- 今日のデモのみCTAを派手にする。",
        "reason": "Demo-only temporary styling.",
        "confidence": 0.9,
    }

    async def keep_temporary(context, payload):
        return types.SimpleNamespace(
            label="keep",
            status="future_relevance",
            reason="Demo-only, time-limited direction.",
            evidence=["今日の社内デモだけ、CTAを少し派手に見せたいです。"],
            evidence_support="supports_memory",
            retention_mode="temporary",
            temporary_expires_at="2026-05-30T00:00:00+09:00",
            confidence=0.91,
        )

    _set_incoming_event(
        ctx,
        event_id="E_MEMORY_TEMP_1",
        message_ts="1777639013.179819",
        text="<@U_ALICE> 今日の社内デモだけ、CTAを少し派手に見せたいです。",
        mentions=["U_ALICE"],
    )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "_get_memory_backend",
        lambda context: memory_backend,
    )
    monkeypatch.setattr(
        chat_conversation_workflow,
        "should_keep_chat_memory_update",
        keep_temporary,
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert len(memory_backend.updates) == 1
    update = memory_backend.updates[0]
    assert update.topic_id.startswith("focusflow-cta-temporary-")
    assert update.retention["status"] == "temporary"
    assert update.retention["kind"] == "temporary"
    assert update.retention["expires_at"] == "2026-05-30T00:00:00+09:00"


@pytest.mark.asyncio
async def test_workflow_promotes_temporary_when_chat_memory_update_returns_should_update_false(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="了解です")
    memory_backend = FakeMemoryBackend(tmp_path / "memory")
    memory_backend.items = [
        MemoryItem(
            id="focusflow-cta",
            title="FocusFlow CTA",
            summary="Current CTA policy",
            path="cognee://guildbotics:person:alice/focusflow-cta",
            content="# FocusFlow CTA\n\n## Decisions\n- CTAは「今日の集中プランを作る」",
            score=1.0,
            match_reason="Matched fake backend memory.",
            scope={"person_id": "alice"},
            retention={"status": "active", "kind": "current_fact"},
        )
    ]
    ctx._memory_update = {
        "should_update": False,
        "topic_id": "focusflow-cta",
        "title": "FocusFlow CTA",
        "summary": "",
        "memory": "",
        "reason": "already reflected",
        "confidence": 0.9,
    }

    async def keep_temporary(context, payload):
        return types.SimpleNamespace(
            label="keep",
            status="future_relevance",
            reason="Demo-only temporary context.",
            evidence=["今日の社内デモだけ、CTAを少し派手に見せたいです。"],
            evidence_support="supports_memory",
            retention_mode="temporary",
            temporary_expires_at="2026-05-30T00:00:00+09:00",
            confidence=0.9,
        )

    _set_incoming_event(
        ctx,
        event_id="E_MEMORY_TEMP_PROMOTE_1",
        message_ts="1777639013.179819",
        text="<@U_ALICE> 今日の社内デモだけ、CTAを少し派手に見せたいです。",
        mentions=["U_ALICE"],
    )

    monkeypatch.setattr(
        chat_conversation_workflow,
        "_get_memory_backend",
        lambda context: memory_backend,
    )
    monkeypatch.setattr(
        chat_conversation_workflow,
        "should_keep_chat_memory_update",
        keep_temporary,
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert len(memory_backend.updates) == 1
    update = memory_backend.updates[0]
    assert update.should_update is True
    assert update.topic_id.startswith("focusflow-cta-temporary-")
    assert update.retention["status"] == "temporary"
    assert update.retention["kind"] == "temporary"
    assert update.retention["expires_at"] == "2026-05-30T00:00:00+09:00"


@pytest.mark.asyncio
async def test_workflow_preserves_speaker_identity_in_transcript_and_reply_input(tmp_path):
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

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert "[agent_1] 別エージェントの発話" in ctx.last_pipe
    assert "[alice] 自分の過去発話" in ctx.last_pipe
    assert ctx.last_chat_reply_input is not None
    assert ctx.last_chat_reply_input["thread_messages"][0] == {
        "content": "別エージェントの発話",
        "author": "agent_1",
        "author_type": "User",
    }
    assert ctx.last_chat_reply_input["thread_messages"][1] == {
        "content": "自分の過去発話",
        "author": "alice",
        "author_type": "Assistant",
    }


@pytest.mark.asyncio
async def test_workflow_uses_runtime_identity_map_for_person_labels(tmp_path):
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

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert "[yuki] 別エージェントの発話" in ctx.last_pipe
    assert ctx.last_chat_reply_input is not None
    assert ctx.last_chat_reply_input["thread_messages"][0]["author"] == "yuki"


@pytest.mark.asyncio
async def test_workflow_does_not_reuse_runtime_person_labels_across_contexts(
    tmp_path
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

    await chat_conversation_workflow.main(
        second_ctx, chat_service=service, state_store=state_store
    )

    assert "[agent_1] 別エージェントの発話" in second_ctx.last_pipe


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

    assert len(ctx.invocations) == EXPECTED_CHAT_INVOCATIONS
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
async def test_workflow_updates_memory_when_should_react_returns_react_only(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = _invoke_context_with_chat_profile(reply_text="リアクションのみで応答します。")
    ctx._thread_context = {
        "thread_topic": "focusflow cta",
        "latest_focus": "cta finalized",
        "reason": "test",
        "confidence": 0.9,
    }
    ctx._memory_update = {
        "should_update": True,
        "topic_id": "focusflow-cta",
        "title": "FocusFlow CTA検討",
        "summary": "CTA finalization",
        "memory": "# FocusFlow CTA検討\n\n## Decisions\n- CTAを確定する。",
        "reason": "decision update",
        "confidence": 0.9,
    }
    _set_incoming_event(
        ctx,
        event_id="E_REACT_ONLY_MEMORY_1",
        message_ts="106.2",
        text="CTAはこの文言で確定でお願いします",
        is_thread_reply=True,
    )

    original_invoke = ctx.invoke

    async def invoke(name: str, /, *args, **kwargs):
        if name == "workflows/chat/should_react":
            ctx.invocations.append((name, args, kwargs))
            return {
                "decision": "react_only",
                "reason": "ack only",
                "reaction": "ack",
            }
        return await original_invoke(name, *args, **kwargs)

    ctx.invoke = invoke

    memory_backend = FakeMemoryBackend(tmp_path / "memory")
    monkeypatch.setattr(
        chat_conversation_workflow,
        "_get_memory_backend",
        lambda context: memory_backend,
    )

    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=state_store)

    assert service.posts == []
    assert service.reactions == [("C1", "106.2", "ack")]
    assert len(memory_backend.updates) == 1
    assert memory_backend.updates[0].topic_id == "focusflow-cta"
    assert memory_backend.updates[0].retention["kind"] == "current_fact"
    assert ctx.last_chat_memory_update_input is not None


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
