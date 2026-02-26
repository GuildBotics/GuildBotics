from __future__ import annotations

import asyncio
import pytest

from guildbotics.drivers.chat_event_source import PollingChatEventSource
from guildbotics.integrations.chat_service import ChatEvent, ChatEventPage, ChatIdentity
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.integrations.slack.slack_socket_mode_chat_event_source import (
    SocketModeChatEventSource,
)
from guildbotics.runtime.event_listener import IncomingChatEvent


class FakeChatService:
    def __init__(self) -> None:
        self.pages: dict[str, ChatEventPage] = {}
        self.calls: list[tuple[str, str | None, str | None]] = []

    async def get_bot_identity(self) -> ChatIdentity:
        return ChatIdentity(user_id="U1")

    async def list_channel_events(
        self, channel_id: str, *, cursor=None, oldest_ts=None, limit: int = 100
    ) -> ChatEventPage:
        self.calls.append((channel_id, cursor, oldest_ts))
        return self.pages.get(channel_id, ChatEventPage())

    async def post_message(self, channel_id: str, text: str, *, thread_ts: str | None = None):
        raise AssertionError("unused in this test")

    async def add_reaction(self, channel_id: str, message_ts: str, reaction: str) -> None:
        raise AssertionError("unused in this test")


class FakePerson:
    def __init__(self, person_id: str = "alice") -> None:
        self.person_id = person_id
        self._secrets = {"SLACK_APP_TOKEN": "xapp-test"}

    def has_secret(self, key: str) -> bool:
        return key in self._secrets

    def get_secret(self, key: str) -> str:
        return self._secrets[key]

    def to_person_env_key(self, key: str) -> str:
        return f"{self.person_id.upper()}_{key}"


@pytest.mark.asyncio
async def test_polling_event_source_fetch_mark_finalize_updates_cursor(tmp_path):
    state_store = FileConversationStateStore(base_dir=tmp_path)
    state_store.save_channel_cursor(
        "slack",
        "alice",
        "C1",
        state=state_store.load_channel_cursor("slack", "alice", "C1"),
    )
    service = FakeChatService()
    service.pages["C1"] = ChatEventPage(
        events=[
            ChatEvent(
                event_id="E1",
                channel_id="C1",
                message_ts="100.1",
                thread_ts="100.1",
                author_id="U2",
                text="hello",
            )
        ],
        cursor="next-1",
        oldest_ts="100.1",
    )
    source = PollingChatEventSource(chat_service=service, state_store=state_store)

    items = await source.fetch_events(
        person_id="alice",
        subscriptions=[{"service": "slack", "channel_id": "C1", "enabled": True}],
    )
    assert len(items) == 1
    assert items[0].service_name == "slack"
    assert items[0].channel_id == "C1"
    assert service.calls == [("C1", None, None)]

    source.mark_processed(person_id="alice", item=items[0])
    source.finalize_cycle(person_id="alice")

    state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert state.cursor == "next-1"
    assert state.oldest_ts == "100.1"
    assert "E1" in state.processed_event_ids


@pytest.mark.asyncio
async def test_polling_event_source_finalizes_cursor_even_without_events(tmp_path):
    state_store = FileConversationStateStore(base_dir=tmp_path)
    state_store.save_channel_cursor(
        "slack",
        "alice",
        "C1",
        state=state_store.load_channel_cursor("slack", "alice", "C1"),
    )
    service = FakeChatService()
    service.pages["C1"] = ChatEventPage(events=[], cursor="next-2", oldest_ts="200.1")
    source = PollingChatEventSource(chat_service=service, state_store=state_store)

    items = await source.fetch_events(
        person_id="alice",
        subscriptions=[{"service": "slack", "channel_id": "C1", "enabled": True}],
    )
    assert items == []

    source.finalize_cycle(person_id="alice")
    state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert state.cursor == "next-2"
    assert state.oldest_ts == "200.1"


@pytest.mark.asyncio
async def test_socket_mode_event_source_wraps_listener_and_manages_pending_events(
    monkeypatch, tmp_path
):
    state_store = FileConversationStateStore(base_dir=tmp_path)
    person = FakePerson("alice")
    created = {"listener": None}

    class _FakeListener:
        def __init__(self, **kwargs):
            created["listener"] = self
            self.started = 0
            self.stopped = 0
            self._drains = [
                [
                    IncomingChatEvent(
                        service_name="slack",
                        channel_id="C1",
                        event=ChatEvent(
                            event_id="C1:100.1",
                            channel_id="C1",
                            message_ts="100.1",
                            thread_ts="100.1",
                            author_id="U2",
                            text="hello",
                        ),
                    ),
                    IncomingChatEvent(
                        service_name="slack",
                        channel_id="C2",
                        event=ChatEvent(
                            event_id="C2:100.2",
                            channel_id="C2",
                            message_ts="100.2",
                            thread_ts="100.2",
                            author_id="U3",
                            text="ignored",
                        ),
                    ),
                ],
                [],
                [],
            ]

        def start(self):
            self.started += 1

        def stop(self):
            self.stopped += 1

        def drain_events(self):
            return self._drains.pop(0) if self._drains else []

    monkeypatch.setattr(
        "guildbotics.integrations.slack.slack_socket_mode_chat_event_source.SlackSocketEventListener",
        _FakeListener,
    )
    source = SocketModeChatEventSource(logger=object(), person=person, state_store=state_store)

    items = await source.fetch_events(
        person_id="alice",
        subscriptions=[
            {
                "service": "slack",
                "channel_id": "C1",
                "enabled": True,
            }
        ],
    )
    assert len(items) == 1
    assert items[0].event.channel_id == "C1"
    assert created["listener"] is not None
    assert created["listener"].started == 1

    # Before mark_processed, durable inbox replays the same unprocessed event.
    items2 = await source.fetch_events(
        person_id="alice",
        subscriptions=[
            {
                "service": "slack",
                "channel_id": "C1",
                "enabled": True,
            }
        ],
    )
    assert [x.event.event_id for x in items2] == ["C1:100.1"]
    assert created["listener"].started == 2

    source.mark_processed(person_id="alice", item=items[0])
    assert state_store.is_processed_event("slack", "alice", "C1", "C1:100.1") is True
    assert state_store.load_pending_events("slack", "alice", "C1") == []

    items3 = await source.fetch_events(
        person_id="alice",
        subscriptions=[
            {
                "service": "slack",
                "channel_id": "C1",
                "enabled": True,
            }
        ],
    )
    assert items3 == []
    await source.aclose()
    assert created["listener"].stopped == 1


@pytest.mark.asyncio
async def test_socket_mode_event_source_requires_app_token(tmp_path):
    state_store = FileConversationStateStore(base_dir=tmp_path)

    class NoTokenPerson(FakePerson):
        def __init__(self) -> None:
            super().__init__("alice")
            self._secrets = {}

    with pytest.raises(ValueError, match="SLACK_APP_TOKEN"):
        SocketModeChatEventSource(
            logger=object(),
            person=NoTokenPerson(),
            state_store=state_store,
        )
