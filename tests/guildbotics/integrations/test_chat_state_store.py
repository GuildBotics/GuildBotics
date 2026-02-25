from __future__ import annotations

from guildbotics.integrations.chat_state_store import (
    ChannelCursorState,
    ScheduledPostState,
    ThreadConversationState,
    ThreadMessageState,
)
from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore


def test_channel_cursor_roundtrip(tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    state = ChannelCursorState(
        cursor="cur-1",
        oldest_ts="123.45",
        processed_event_ids=["e1", "e2"],
    )

    store.save_channel_cursor("slack", "alice", "C1", state)
    loaded = store.load_channel_cursor("slack", "alice", "C1")

    assert loaded.cursor == "cur-1"
    assert loaded.oldest_ts == "123.45"
    assert loaded.processed_event_ids == ["e1", "e2"]


def test_mark_processed_event_and_dedupes(tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)

    store.mark_processed_event("slack", "alice", "C1", "e1")
    store.mark_processed_event("slack", "alice", "C1", "e2")
    store.mark_processed_event("slack", "alice", "C1", "e1")

    assert store.is_processed_event("slack", "alice", "C1", "e1") is True
    assert store.is_processed_event("slack", "alice", "C1", "e2") is True
    assert store.is_processed_event("slack", "alice", "C1", "e3") is False

    loaded = store.load_channel_cursor("slack", "alice", "C1")
    assert loaded.processed_event_ids == ["e1", "e2"]


def test_processed_event_limit_keeps_most_recent_unique(tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path, max_processed_events=3)
    for event_id in ["e1", "e2", "e3", "e4"]:
        store.mark_processed_event("slack", "alice", "C1", event_id)

    loaded = store.load_channel_cursor("slack", "alice", "C1")
    assert loaded.processed_event_ids == ["e2", "e3", "e4"]


def test_thread_state_roundtrip(tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    state = ThreadConversationState(
        channel_id="C1",
        thread_ts="123.456",
        participants={"alice", "bob"},
        last_bot_replier_id="alice",
        response_expected=False,
        thread_claimed_by_other=True,
    )

    store.save_thread_state("slack", "alice", "C1", "123.456", state)
    loaded = store.load_thread_state("slack", "alice", "C1", "123.456")

    assert loaded.channel_id == "C1"
    assert loaded.thread_ts == "123.456"
    assert loaded.participants == {"alice", "bob"}
    assert loaded.last_bot_replier_id == "alice"
    assert loaded.response_expected is False
    assert loaded.thread_claimed_by_other is True


def test_load_missing_state_returns_defaults(tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)

    channel = store.load_channel_cursor("slack", "alice", "C1")
    assert channel.cursor is None
    assert channel.oldest_ts is None
    assert channel.processed_event_ids == []

    thread = store.load_thread_state("slack", "alice", "C1", "100.1")
    assert thread.channel_id == "C1"
    assert thread.thread_ts == "100.1"
    assert thread.participants == set()
    assert thread.last_bot_replier_id is None
    assert thread.response_expected is True
    assert thread.thread_claimed_by_other is False
    assert store.load_thread_messages("slack", "alice", "C1", "100.1") == []


def test_state_is_isolated_per_person_and_channel(tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.mark_processed_event("slack", "alice", "C1", "e1")
    store.mark_processed_event("slack", "bob", "C1", "e2")
    store.mark_processed_event("slack", "alice", "C2", "e3")

    assert store.is_processed_event("slack", "alice", "C1", "e1") is True
    assert store.is_processed_event("slack", "alice", "C1", "e2") is False
    assert store.is_processed_event("slack", "bob", "C1", "e2") is True
    assert store.is_processed_event("slack", "alice", "C2", "e3") is True


def test_scheduled_post_state_roundtrip_and_default(tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)

    default_state = store.load_scheduled_post_state("slack", "alice", "morning-topic")
    assert default_state.last_run_slot is None

    store.save_scheduled_post_state(
        "slack",
        "alice",
        "morning-topic",
        ScheduledPostState(last_run_slot="2026-02-23T09:00"),
    )
    loaded = store.load_scheduled_post_state("slack", "alice", "morning-topic")
    assert loaded.last_run_slot == "2026-02-23T09:00"


def test_thread_messages_roundtrip_replace_and_trim(tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path, max_thread_messages=2)
    store.append_thread_message(
        "slack",
        "alice",
        "C1",
        "100.1",
        ThreadMessageState(
            channel_id="C1",
            thread_ts="100.1",
            message_ts="100.1",
            author_id="U1",
            text="root",
            mentions=[],
            is_bot_message=False,
        ),
    )
    store.append_thread_message(
        "slack",
        "alice",
        "C1",
        "100.1",
        ThreadMessageState(
            channel_id="C1",
            thread_ts="100.1",
            message_ts="100.2",
            author_id="U2",
            text="reply",
            mentions=["U1"],
            is_bot_message=False,
        ),
    )
    store.append_thread_message(
        "slack",
        "alice",
        "C1",
        "100.1",
        ThreadMessageState(
            channel_id="C1",
            thread_ts="100.1",
            message_ts="100.2",
            author_id="U2",
            text="reply updated",
            mentions=["U1"],
            is_bot_message=False,
        ),
    )
    store.append_thread_message(
        "slack",
        "alice",
        "C1",
        "100.1",
        ThreadMessageState(
            channel_id="C1",
            thread_ts="100.1",
            message_ts="100.3",
            author_id="U_BOT",
            text="bot",
            mentions=[],
            is_bot_message=True,
        ),
    )

    loaded = store.load_thread_messages("slack", "alice", "C1", "100.1")
    assert [m.message_ts for m in loaded] == ["100.2", "100.3"]
    assert loaded[0].text == "reply updated"
    assert loaded[1].is_bot_message is True


def test_pending_events_roundtrip_dedupe_and_remove(tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path, max_processed_events=3)
    event1 = ChatEvent(
        event_id="C1:100.1",
        channel_id="C1",
        message_ts="100.1",
        thread_ts="100.1",
        author_id="U1",
        text="hello",
        mentions=[],
    )
    event1_updated = ChatEvent(
        event_id="C1:100.1",
        channel_id="C1",
        message_ts="100.1",
        thread_ts="100.1",
        author_id="U1",
        text="hello updated",
        mentions=["U2"],
    )
    event2 = ChatEvent(
        event_id="C1:100.2",
        channel_id="C1",
        message_ts="100.2",
        thread_ts="100.1",
        author_id="U2",
        text="reply",
        mentions=[],
        is_thread_reply=True,
    )
    store.upsert_pending_event("slack", "alice", "C1", event1)
    store.upsert_pending_event("slack", "alice", "C1", event1_updated)
    store.upsert_pending_event("slack", "alice", "C1", event2)
    loaded = store.load_pending_events("slack", "alice", "C1")
    assert [e.event_id for e in loaded] == ["C1:100.1", "C1:100.2"]
    assert loaded[0].text == "hello updated"
    assert loaded[0].mentions == ["U2"]

    store.remove_pending_event("slack", "alice", "C1", "C1:100.1")
    loaded2 = store.load_pending_events("slack", "alice", "C1")
    assert [e.event_id for e in loaded2] == ["C1:100.2"]
