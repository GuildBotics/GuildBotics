from __future__ import annotations

import asyncio
import threading
import time
import pytest
import httpx

from guildbotics.drivers.chat_event_source import PollingChatEventSource
from guildbotics.integrations.chat_service import ChatEvent, ChatEventPage, ChatIdentity
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.integrations.slack.slack_socket_mode_chat_event_source import (
    SocketModeChatEventSource,
)


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


class FakeSocket:
    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.sent: list[str] = []
        self.closed = False
        self._lock = threading.Lock()
        self._wait = threading.Condition(self._lock)

    def recv(self) -> str:
        deadline = time.time() + 1.0
        with self._wait:
            while not self._frames and not self.closed:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise RuntimeError("fake socket recv timeout")
                self._wait.wait(timeout=remaining)
            if self.closed:
                raise RuntimeError("socket closed")
            return self._frames.pop(0)

    def send(self, text: str) -> None:
        with self._wait:
            self.sent.append(text)
            self._wait.notify_all()

    def close(self) -> None:
        with self._wait:
            self.closed = True
            self._wait.notify_all()


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
async def test_socket_mode_event_source_fetches_and_acks_events(tmp_path):
    state_store = FileConversationStateStore(base_dir=tmp_path)
    person = FakePerson("alice")
    frames = [
        '{"type":"hello"}',
        (
            '{"envelope_id":"env-1","type":"events_api","payload":{"event":{"type":"message",'
            '"channel":"C1","user":"U2","text":"<@UALICE1> hi","ts":"100.1"}}}'
        ),
        (
            '{"envelope_id":"env-2","type":"events_api","payload":{"event":{"type":"message",'
            '"channel":"C2","user":"U3","text":"ignored","ts":"100.2"}}}'
        ),
    ]
    fake_ws = FakeSocket(frames)
    open_calls = 0

    def ws_connect(url: str):
        assert url == "wss://example/socket"
        return fake_ws

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal open_calls
        assert request.url.path.endswith("/apps.connections.open")
        open_calls += 1
        return httpx.Response(200, json={"ok": True, "url": "wss://example/socket"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = SocketModeChatEventSource(
        logger=object(),
        person=person,
        state_store=state_store,
        http_client=client,
        ws_connect=ws_connect,
    )

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
    if not items:
        await asyncio.sleep(0.05)
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
    assert items[0].event.mentions == ["UALICE1"]
    assert json_contains_envelope(fake_ws.sent, "env-1")
    assert open_calls == 1

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
    if not json_contains_envelope(fake_ws.sent, "env-2"):
        await asyncio.sleep(0.05)
    assert json_contains_envelope(fake_ws.sent, "env-2")
    assert open_calls == 1

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
    assert fake_ws.closed is True
    client.close()


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


def json_contains_envelope(sent_payloads: list[str], envelope_id: str) -> bool:
    needle = f'"envelope_id": "{envelope_id}"'
    return any(needle in payload for payload in sent_payloads)
