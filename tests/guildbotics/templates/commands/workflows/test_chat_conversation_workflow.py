from __future__ import annotations

import types

import pytest

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.integrations.chat_service import ChatEvent, ChatIdentity
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.runtime.event_listener import (
    INCOMING_CHAT_EVENT_KEY,
    IncomingChatEvent,
)
from guildbotics.templates.commands.workflows import chat_conversation_workflow


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("GUILDBOTICS_DATA_DIR", str(tmp_path / "data"))


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

    async def get_bot_identity(self) -> ChatIdentity:
        return self.identity

    def normalize_participant_text(self, text, participant_labels):
        for user_id, label in participant_labels.items():
            text = text.replace(f"<@{user_id}>", f"@{label}")
        return text

    def render_participant_text(self, text, participant_labels):
        return text


class FakeInvokeContext(types.SimpleNamespace):
    def __init__(self, action: str) -> None:
        person = types.SimpleNamespace(
            person_id="alice",
            name="Alice",
            profile={"chat": {"subscriptions": [{"service": "slack"}]}},
        )
        super().__init__(
            person=person,
            logger=StubLogger(),
            language_name="日本語",
            shared_state={},
        )
        self.action = action
        self.invocations: list[tuple[str, dict]] = []

    async def invoke(self, name: str, /, **kwargs):
        self.invocations.append((name, kwargs))
        if name != "functions/handle_chat_event":
            return {}
        run_id = kwargs["workflow_run_id"]
        store = RunStore()
        if self.action == "reply":
            store.append_evidence(
                run_id,
                "chat_reply",
                {
                    "service": "slack",
                    "channel_id": kwargs["channel_id"],
                    "message_ts": "200.1",
                    "thread_ts": kwargs["thread_ts"],
                    "text": "確認します。",
                    "posted": True,
                },
            )
            store.complete_run(
                run_id,
                "done",
                "Posted a reply.",
                subject_type="chat",
                subject_id=(
                    f"slack:{kwargs['channel_id']}:"
                    f"{kwargs['thread_ts']}:{kwargs['event_id']}"
                ),
                person_id=kwargs["person_id"],
            )
        elif self.action == "reaction":
            store.append_evidence(
                run_id,
                "chat_reaction",
                {
                    "service": "slack",
                    "channel_id": kwargs["channel_id"],
                    "message_ts": kwargs["message_ts"],
                    "reaction": "ack",
                    "reacted": True,
                },
            )
            store.complete_run(
                run_id,
                "done",
                "Added a reaction.",
                subject_type="chat",
                subject_id=(
                    f"slack:{kwargs['channel_id']}:"
                    f"{kwargs['thread_ts']}:{kwargs['event_id']}"
                ),
                person_id=kwargs["person_id"],
            )
        elif self.action == "noop":
            store.append_evidence(
                run_id,
                "chat_noop",
                {
                    "service": "slack",
                    "channel_id": kwargs["channel_id"],
                    "thread_ts": kwargs["thread_ts"],
                    "event_id": kwargs["event_id"],
                    "reason": "No response needed.",
                    "noop": True,
                },
            )
            store.complete_run(
                run_id,
                "done",
                "No response needed.",
                subject_type="chat",
                subject_id=(
                    f"slack:{kwargs['channel_id']}:"
                    f"{kwargs['thread_ts']}:{kwargs['event_id']}"
                ),
                person_id=kwargs["person_id"],
            )
        return {"status": "done", "message": "done"}


def _set_incoming_event(
    ctx: types.SimpleNamespace,
    *,
    event_id: str = "E1",
    text: str = "<@U_ALICE> please check",
    mentions: list[str] | None = None,
) -> None:
    ctx.shared_state[INCOMING_CHAT_EVENT_KEY] = IncomingChatEvent(
        service_name="slack",
        channel_id="C1",
        event=ChatEvent(
            event_id=event_id,
            channel_id="C1",
            message_ts="100.1",
            thread_ts="100.1",
            author_id="U_USER",
            text=text,
            mentions=list(mentions if mentions is not None else ["U_ALICE"]),
        ),
    ).to_shared_state()


@pytest.mark.asyncio
async def test_workflow_delegates_to_handle_chat_event_and_updates_reply_state(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(ctx)

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert service.posts == []
    assert service.reactions == []
    assert ctx.invocations[0][0] == "functions/handle_chat_event"
    kwargs = ctx.invocations[0][1]
    assert kwargs["person_id"] == "alice"
    assert kwargs["service_name"] == "slack"
    assert kwargs["channel_id"] == "C1"
    assert kwargs["cli_agent_env"]["GUILDBOTICS_RUN_ID"] == kwargs["workflow_run_id"]
    assert kwargs["cwd"].name == "alice"
    # The capability reference is no longer injected per-prompt; the agent reads
    # it from the mandatory `member context` call (the single source of truth).
    assert "chat_capability_help" not in kwargs

    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E1"]
    thread_messages = state_store.load_thread_messages("slack", "alice", "C1", "100.1")
    assert [message.message_ts for message in thread_messages] == ["100.1", "200.1"]
    assert thread_messages[1].is_bot_message is True
    thread_state = state_store.load_thread_state("slack", "alice", "C1", "100.1")
    assert "alice" in thread_state.participants


@pytest.mark.asyncio
async def test_reaction_only_completion_processes_without_bot_message(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reaction")
    _set_incoming_event(ctx)

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E1"]
    thread_messages = state_store.load_thread_messages("slack", "alice", "C1", "100.1")
    assert [message.message_ts for message in thread_messages] == ["100.1"]
    # A reaction is a visible action, so the member is recorded as a participant.
    thread_state = state_store.load_thread_state("slack", "alice", "C1", "100.1")
    assert "alice" in thread_state.participants


@pytest.mark.asyncio
async def test_noop_completion_processes_without_visible_action(tmp_path, monkeypatch):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(ctx)

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E1"]
    # noop takes no visible action, so the member must not be recorded as a
    # thread participant.
    thread_state = state_store.load_thread_state("slack", "alice", "C1", "100.1")
    assert "alice" not in thread_state.participants


@pytest.mark.asyncio
async def test_missing_completion_is_not_processed(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("missing")
    _set_incoming_event(ctx)

    with pytest.raises(Exception, match="not found|not completed"):
        await chat_conversation_workflow.main(
            ctx, chat_service=service, state_store=state_store
        )

    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == []


@pytest.mark.asyncio
async def test_obvious_self_message_is_marked_processed_without_agent(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    ctx.shared_state[INCOMING_CHAT_EVENT_KEY] = IncomingChatEvent(
        service_name="slack",
        channel_id="C1",
        event=ChatEvent(
            event_id="E_SELF",
            channel_id="C1",
            message_ts="100.1",
            thread_ts="100.1",
            author_id="U_ALICE",
            text="bot message",
        ),
    ).to_shared_state()

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E_SELF"]
