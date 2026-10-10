"""What the local chat does beyond the chat port's contract: it is files a
person or a test writes, so it must take a line it cannot read in its stride."""

from __future__ import annotations

import logging
import time

from guildbotics.integrations.local import chat
from guildbotics.integrations.local.chat import LocalChatListener
from tests.guildbotics.local_chat import at, say


def test_the_listener_skips_a_line_that_is_no_message_and_keeps_listening():
    say("C1", "before", message_id="m0", occurred_at=at(0))
    listener = LocalChatListener(logging.getLogger(__name__), lambda: None)
    listener.start()
    try:
        with chat.channel_path("C1").open("a", encoding="utf-8") as file:
            file.write("not json\n")
            # Half a line: the rest is still being written.
            file.write('{"message_id": "m2", "occurred_at": ')
        say("C2", "elsewhere", message_id="m1", occurred_at=at(1))
        received = []
        deadline = time.monotonic() + 5
        while not received and time.monotonic() < deadline:
            received.extend(listener.drain_events())
            time.sleep(0.02)
        assert [e.message_id for e in received] == ["m1"]
        assert listener.connected
    finally:
        listener.stop()
