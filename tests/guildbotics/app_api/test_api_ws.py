"""Integration tests for the `/events` websocket endpoint.

These tests exercise the real endpoint wiring in
``guildbotics.app_api.api.create_app`` by driving the shared ``EventBus`` the
same way the application does (publishing events through the bus that is
injected into the app). They assert concrete message payloads, the
policy-violation close on a bad token, replayed history content, delivery to an
already-connected client, subscription cleanup after disconnect, and that the
token never reaches what the server logs.
"""

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from guildbotics.app_api.api import _stream, create_app
from guildbotics.app_api.events import EventBus
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.utils.correlation import trace_scope
from guildbotics.utils.local_api import TOKEN_SUBPROTOCOL

POLICY_VIOLATION_CLOSE_CODE = 1008
FORBIDDEN = 403
TOKEN = "secret"


class RuntimeStub:
    """Minimal runtime stub: websocket endpoints never touch it."""

    def stop_scheduler(self, *, force: bool = False) -> None:
        return None

    async def close_cli_agent_usage(self) -> None:
        return None


def _app(event_bus: EventBus, store: DiagnosticsStore | None = None):
    return create_app(
        session_token=TOKEN,
        runtime=RuntimeStub(),
        event_bus=event_bus,
        diagnostics_store=store,
    )


def _connect(client: TestClient):
    return client.websocket_connect("/events", subprotocols=[TOKEN_SUBPROTOCOL, TOKEN])


def test_events_success_receives_published_event(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)

    with TestClient(app) as client, _connect(client) as websocket:
        with trace_scope("manual", trace_id="request-live"):
            event_bus.publish_event(
                "command.started",
                {"command": "hello"},
            )
        event = websocket.receive_json()

    # The fixed name is selected, so the token is never echoed back.
    assert websocket.accepted_subprotocol == TOKEN_SUBPROTOCOL
    assert event["type"] == "command.started"
    assert event["trace_id"] == "request-live"
    assert event["payload"] == {"command": "hello"}
    assert event["timestamp"]


@pytest.mark.parametrize(
    ("path", "subprotocols"),
    [
        ("/events", [TOKEN_SUBPROTOCOL, "wrong"]),
        ("/events", [TOKEN, TOKEN_SUBPROTOCOL]),
        ("/events", [TOKEN]),
        ("/events", [TOKEN_SUBPROTOCOL]),
        ("/events", [TOKEN_SUBPROTOCOL, TOKEN, "extra"]),
        ("/events", []),
        # The URL is no carrier anymore.
        (f"/events?token={TOKEN}", []),
    ],
    ids=[
        "wrong-token",
        "reversed",
        "token-alone",
        "name-alone",
        "extra",
        "none",
        "url-query",
    ],
)
def test_events_without_the_token_subprotocol_closes_with_policy_violation(
    path: str, subprotocols: list[str]
) -> None:
    event_bus = EventBus()
    app = _app(event_bus)
    # A socket accepted by mistake receives this at once instead of waiting.
    event_bus.publish_event("command.started", {})

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(path, subprotocols=subprotocols) as websocket,
    ):
        websocket.receive_json()

    assert exc_info.value.code == POLICY_VIOLATION_CLOSE_CODE
    # No subscriber was ever registered for a rejected connection.
    assert event_bus._event_subscribers == set()


def test_events_history_is_replayed_on_connect(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)
    # Publish before any client connects: the item must live in history.
    with trace_scope("manual", trace_id="request-history"):
        event_bus.publish_event(
            "command.finished",
            {"output": "done"},
        )

    with TestClient(app) as client, _connect(client) as websocket:
        replayed = websocket.receive_json()

    assert replayed["type"] == "command.finished"
    assert replayed["trace_id"] == "request-history"
    assert replayed["payload"] == {"output": "done"}


def test_events_history_then_live_delivery_in_order(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)
    with trace_scope("manual", trace_id="r1"):
        event_bus.publish_event("command.started", {"command": "a"})

    with TestClient(app) as client, _connect(client) as websocket:
        first = websocket.receive_json()
        with trace_scope("manual", trace_id="r1"):
            event_bus.publish_event("command.finished", {"command": "a"})
        second = websocket.receive_json()

    assert first["type"] == "command.started"
    assert second["type"] == "command.finished"
    assert [first["trace_id"], second["trace_id"]] == ["r1", "r1"]


def test_disconnect_closes_event_subscription(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)

    with TestClient(app) as client:
        with _connect(client) as websocket:
            event_bus.publish_event("command.started", {})
            websocket.receive_json()
            assert len(event_bus._event_subscribers) == 1

        # After the context manager exits the client has disconnected; the
        # endpoint's ``finally: queue.close()`` must drop the subscriber.
        _wait_for_no_subscribers(event_bus._event_subscribers)

    assert event_bus._event_subscribers == set()
    # A further publish after disconnect must not raise.
    event_bus.publish_event("command.finished", {})


def _wait_for_no_subscribers(subscribers: set, timeout: float = 2.0) -> None:
    """Poll until the endpoint thread has run its cleanup, bounded by timeout."""
    deadline = time.monotonic() + timeout
    while subscribers and time.monotonic() < deadline:
        time.sleep(0.01)


def test_stream_ends_on_disconnect_while_no_event_is_pending() -> None:
    """A closed socket must end the handler without waiting for an event.

    The server closes every websocket when it shuts down and then waits for the
    handlers; one that only noticed the close on its next send would hold that
    shutdown open for as long as the bus stays quiet.
    """
    event_bus = EventBus()

    class ClosedSocket:
        scope = {"subprotocols": [TOKEN_SUBPROTOCOL, TOKEN]}

        async def accept(self, subprotocol: str) -> None:
            return None

        async def receive(self) -> dict[str, object]:
            return {"type": "websocket.disconnect", "code": 1012}

        async def send_json(self, item: object) -> None:
            raise AssertionError("no event was published")

    async def run() -> None:
        await asyncio.wait_for(
            _stream(ClosedSocket(), TOKEN, event_bus),
            timeout=5,
        )

    asyncio.run(run())

    assert event_bus._event_subscribers == set()


class _Lines(logging.Handler):
    """Stands in for the sidecar's stderr, which Desktop keeps as its log."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def test_the_token_reaches_neither_the_server_log_nor_the_diagnostics(
    tmp_path: Path,
) -> None:
    """uvicorn logs every handshake with its path and query, and the app hands
    that logger to the diagnostics; only a real server writes those lines."""
    token = "session-token-763"
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    event_bus = EventBus(store=store)
    app = create_app(
        session_token=token,
        runtime=RuntimeStub(),
        event_bus=event_bus,
        diagnostics_store=store,
    )
    logger = logging.getLogger("uvicorn.error")
    stderr = _Lines()
    level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(stderr)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None)
    try:
        with config.bind_socket() as sock:
            server = uvicorn.Server(config)
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [sock]}, daemon=True
            )
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    assert time.monotonic() < deadline, "server startup timed out"
                    time.sleep(0.01)
                url = f"ws://127.0.0.1:{sock.getsockname()[1]}/events"
                with connect(url, subprotocols=[TOKEN_SUBPROTOCOL, token]) as socket:
                    selected = socket.subprotocol
                with (
                    pytest.raises(InvalidStatus) as rejected,
                    connect(url, subprotocols=[TOKEN_SUBPROTOCOL, "wrong"]),
                ):
                    pass
            finally:
                server.should_exit = True
                thread.join(timeout=10)
    finally:
        logger.removeHandler(stderr)
        logger.setLevel(level)

    assert selected == TOKEN_SUBPROTOCOL
    assert rejected.value.response.status_code == FORBIDDEN
    [session] = (tmp_path / "sessions").glob("system-*.jsonl")
    recorded = [json.loads(line) for line in session.read_text("utf-8").splitlines()]
    logged = {
        "stderr": "\n".join(stderr.lines),
        "diagnostics": "\n".join(item.get("message", "") for item in recorded),
    }
    # Both channels carried the handshakes, so their silence about the token
    # is not for want of lines.
    for channel, lines in logged.items():
        assert '"WebSocket /events" [accepted]' in lines, channel
        assert '"WebSocket /events" 403' in lines, channel
        assert token not in lines, channel
    for path in tmp_path.rglob("*"):
        assert not path.is_file() or token not in path.read_text("utf-8"), path
