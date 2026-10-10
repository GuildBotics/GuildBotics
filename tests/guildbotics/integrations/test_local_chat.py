"""What the local chat does beyond the chat port's contract: it is files a
person or a test writes, so a line it cannot read is passed over alone, and
whatever else was written with it still arrives."""

from __future__ import annotations

import json
import logging
import time

import pytest

from guildbotics.entities.team import Person
from guildbotics.integrations.local import chat
from guildbotics.integrations.local.chat import LocalChatListener, LocalChatService
from guildbotics.runtime.chat_service import ChatEvent, ChatServiceError
from tests.guildbotics.local_chat import at, say

#: Lines no message or reaction is recorded in.
_NOT_RECORDS = [
    "not json",
    "null",
    "[1, 2]",
    '{"text": "no id", "occurred_at": "2026-10-01T00:00:00+00:00"}',
    '{"message_id": "x", "occurred_at": "yesterday"}',
    '{"message_id": null, "occurred_at": "2026-10-01T00:00:00+00:00"}',
    '{"reaction": "ack"}',
    '{"message_id": "x", "occurred_at": "2026-10-01T00:00:00"}',
]


def _line(message_id: str, seconds: float, text: str) -> str:
    return json.dumps(
        {
            "message_id": message_id,
            "thread_id": message_id,
            "occurred_at": at(seconds).isoformat(),
            "author": "otota",
            "text": text,
        }
    )


def _write(channel_id: str, *lines: str, end: str = "\n") -> None:
    path = chat.channel_path(channel_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write("\n".join(lines) + end)


def _receive(listener: LocalChatListener, count: int) -> list[ChatEvent]:
    received: list[ChatEvent] = []
    deadline = time.monotonic() + 5
    while len(received) < count and time.monotonic() < deadline:
        received.extend(listener.drain_events())
        time.sleep(0.02)
    return received


@pytest.mark.parametrize("bad", _NOT_RECORDS)
def test_the_listener_passes_over_a_bad_line_alone_and_keeps_listening(bad):
    say("C1", "before", message_id="m0", occurred_at=at(0))
    say("C2", "before", message_id="n0", occurred_at=at(0))
    listener = LocalChatListener(logging.getLogger(__name__), lambda: None)
    listener.start()
    try:
        # Read in one poll: good, bad, good -- and another channel with them.
        _write("C1", _line("m1", 1, "first"), bad, _line("m2", 2, "second"))
        _write("C2", _line("n1", 3, "elsewhere"))
        received = _receive(listener, 3)
        # A line still being written arrives once it is whole.
        _write("C1", _line("m3", 4, "late")[:20], end="")
        time.sleep(0.2)
        _write("C1", _line("m3", 4, "late")[20:])
        received += _receive(listener, 1)
        assert listener.connected
    finally:
        listener.stop()

    assert sorted((e.channel_id, e.message_id) for e in received) == [
        ("C1", "m1"),
        ("C1", "m2"),
        ("C1", "m3"),
        ("C2", "n1"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _NOT_RECORDS)
async def test_reading_a_channel_passes_over_a_bad_line(bad):
    aiko = Person(person_id="aiko", name="Aiko")
    _write("C1", _line("t1", 1, "root"), bad, _line("t2", 2, "next"))
    service = LocalChatService(aiko)

    channel = await service.list_channel_events("C1")
    thread = await service.list_thread_events("C1", thread_id="t1")
    await service.add_reaction("C1", "t2", "ack")

    assert [e.message_id for e in channel.events] == ["t1", "t2"]
    assert [e.message_id for e in thread.events] == ["t1"]


@pytest.mark.asyncio
async def test_a_cursor_no_page_gave_is_refused():
    say("C1", "root", message_id="t1", occurred_at=at(1))

    for cursor in ("abc", "²"):
        with pytest.raises(ChatServiceError, match="cursor"):
            await LocalChatService(
                Person(person_id="aiko", name="Aiko")
            ).list_channel_events("C1", cursor=cursor)
