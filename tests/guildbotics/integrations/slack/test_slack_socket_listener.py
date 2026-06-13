from __future__ import annotations

import json
import threading
import time

import httpx

from guildbotics.integrations.slack.slack_socket_listener import (
    SlackSocketEventListener,
)


class _FakeSocket:
    def __init__(self, frames: list[str], *, fail_on_empty: bool = False) -> None:
        self._frames = list(frames)
        self._fail_on_empty = fail_on_empty
        self.sent: list[str] = []
        self.closed = False
        self._cond = threading.Condition()

    def recv(self) -> str:
        with self._cond:
            while not self.closed:
                if self._frames:
                    return self._frames.pop(0)
                if self._fail_on_empty:
                    raise RuntimeError("simulated socket failure")
                self._cond.wait(timeout=0.05)
            raise RuntimeError("socket closed")

    def send(self, text: str) -> None:
        with self._cond:
            self.sent.append(text)

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()


def _dummy_logger():
    class _L:
        def debug(self, *args, **kwargs):
            return None

    return _L()


def test_slack_socket_listener_reconnects_and_keeps_drained_events(monkeypatch):
    monkeypatch.setattr(
        "guildbotics.integrations.slack.slack_socket_listener.time.sleep",
        lambda _s: None,
    )

    open_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        open_calls["count"] += 1
        return httpx.Response(200, json={"ok": True, "url": "wss://example/socket"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    ws1 = _FakeSocket(
        [
            json.dumps(
                {
                    "envelope_id": "env-1",
                    "type": "events_api",
                    "payload": {
                        "event": {
                            "type": "message",
                            "channel": "C1",
                            "user": "U1",
                            "text": "hello",
                            "ts": "100.1",
                        }
                    },
                }
            )
        ],
        fail_on_empty=True,
    )
    ws2 = _FakeSocket([], fail_on_empty=False)
    ws_connect_calls = {"count": 0}

    def ws_connect(url: str):
        assert url == "wss://example/socket"
        ws_connect_calls["count"] += 1
        if ws_connect_calls["count"] == 1:
            return ws1
        return ws2

    listener = SlackSocketEventListener(
        logger=_dummy_logger(),
        app_token="xapp-test",
        http_client=client,
        ws_connect=ws_connect,
    )

    listener.start()
    deadline = time.time() + 2.0
    drained = []
    while time.time() < deadline:
        drained.extend(listener.drain_events())
        if drained and ws_connect_calls["count"] >= 2:
            break
        time.sleep(0.01)

    listener.stop()
    client.close()

    assert ws_connect_calls["count"] >= 2
    assert open_calls["count"] >= 2
    assert ws1.closed is True
    assert ws2.closed is True
    assert [item.event.event_id for item in drained] == ["C1:100.1"]
    assert any("env-1" in msg for msg in ws1.sent)


def test_to_incoming_event_ignores_message_changed_and_deleted():
    listener = SlackSocketEventListener(
        logger=_dummy_logger(),
        app_token="xapp-test",
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _req: httpx.Response(500))
        ),
        ws_connect=lambda _url: _FakeSocket([]),
    )

    changed = {
        "type": "events_api",
        "payload": {
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "channel": "C1",
                "ts": "100.1",
                "text": "edited",
            }
        },
    }
    deleted = {
        "type": "events_api",
        "payload": {
            "event": {
                "type": "message",
                "subtype": "message_deleted",
                "channel": "C1",
                "ts": "100.2",
            }
        },
    }

    assert listener._to_incoming_event(changed) is None
    assert listener._to_incoming_event(deleted) is None
