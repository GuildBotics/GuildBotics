"""Integration tests for the `/events` websocket endpoint.

These tests exercise the real endpoint wiring in
``guildbotics.app_api.api.create_app`` by driving the shared ``EventBus`` the
same way the application does (publishing events through the bus that is
injected into the app). They assert concrete message payloads, the
policy-violation close without a session from this server's own origin,
replayed history content, delivery to an already-connected client,
subscription cleanup after disconnect, and that no credential reaches what the
server logs.
"""

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from guildbotics.app_api.api import _stream, create_app
from guildbotics.app_api.auth import ServerAuth

COOKIE = "guildbotics_session_8765"
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.workspace_sync import WorkspaceSyncService
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.utils.correlation import trace_scope
from guildbotics.utils.local_api import API_PREFIX, base_url, server_directory
from tests.guildbotics.app_api.owner import (
    EVENTS_URL,
    ORIGIN,
    browser_client,
    owner_client,
    owner_token,
)

POLICY_VIOLATION_CLOSE_CODE = 1008
FORBIDDEN = 403


class RuntimeStub:
    """Minimal runtime stub: websocket endpoints never touch it."""

    system_service_run_id = "stub-service"

    def __init__(self, event_bus: EventBus) -> None:
        self.workspace_sync_service = WorkspaceSyncService()
        self.event_bus = event_bus

    def stop_scheduler(self, *, force: bool = False) -> None:
        return None

    async def close_cli_agent_usage(self) -> None:
        return None

    def saved_team(self) -> None:
        raise AssertionError("websocket endpoints never read the team")


def _app(event_bus: EventBus):
    return create_app(runtime=RuntimeStub(event_bus))


def _connect(client: TestClient):
    return client.websocket_connect(EVENTS_URL)


def test_events_success_receives_published_event(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)

    with browser_client(app) as client, _connect(client) as websocket:
        with trace_scope("manual", trace_id="request-live"):
            event_bus.publish_event(
                "command.started",
                {"command": "hello"},
            )
        event = websocket.receive_json()

    # The session is the credential: no subprotocol carries anything.
    assert websocket.accepted_subprotocol is None
    assert event["type"] == "command.started"
    assert event["trace_id"] == "request-live"
    assert event["payload"] == {"command": "hello"}
    assert event["timestamp"]


def _without_cookie(app: FastAPI) -> tuple[TestClient, dict[str, str]]:
    return TestClient(app), {"Origin": ORIGIN}


def _without_origin(app: FastAPI) -> tuple[TestClient, dict[str, str]]:
    return TestClient(app, cookies=browser_client(app).cookies), {}


def _from_another_origin(app: FastAPI) -> tuple[TestClient, dict[str, str]]:
    client = TestClient(app, cookies=browser_client(app).cookies)
    return client, {"Origin": "http://127.0.0.1:1420"}


def _unknown_cookie(app: FastAPI) -> tuple[TestClient, dict[str, str]]:
    return TestClient(app, cookies={COOKIE: "unknown"}), {"Origin": ORIGIN}


def _owner_token(app: FastAPI) -> tuple[TestClient, dict[str, str]]:
    # A Bearer token is for the CLI and the Desktop host, which do not listen.
    return owner_client(app), {"Origin": ORIGIN}


def _token_subprotocol(app: FastAPI) -> tuple[TestClient, dict[str, str]]:
    # How the token used to travel: it is no credential any more.
    protocols = f"guildbotics.session, {owner_token(app)}"
    return TestClient(app), {"Origin": ORIGIN, "Sec-WebSocket-Protocol": protocols}


@pytest.mark.parametrize(
    "refused",
    [
        _without_cookie,
        _without_origin,
        _from_another_origin,
        _unknown_cookie,
        _owner_token,
        _token_subprotocol,
    ],
    ids=[
        "no-session",
        "no-origin",
        "another-origin",
        "unknown-session",
        "bearer",
        "token-subprotocol",
    ],
)
def test_events_without_a_session_from_this_origin_closes_with_policy_violation(
    refused,
) -> None:
    event_bus = EventBus()
    app = _app(event_bus)
    # A socket accepted by mistake receives this at once instead of waiting.
    event_bus.publish_event("command.started", {})
    client, headers = refused(app)

    with (
        client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(EVENTS_URL, headers=headers) as websocket,
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

    with browser_client(app) as client, _connect(client) as websocket:
        replayed = websocket.receive_json()

    assert replayed["type"] == "command.finished"
    assert replayed["trace_id"] == "request-history"
    assert replayed["payload"] == {"output": "done"}


def test_events_history_then_live_delivery_in_order(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)
    with trace_scope("manual", trace_id="r1"):
        event_bus.publish_event("command.started", {"command": "a"})

    with browser_client(app) as client, _connect(client) as websocket:
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

    with browser_client(app) as client:
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

    class Admitting:
        def admits(self, websocket: object) -> bool:
            return True

    class ClosedSocket:
        async def accept(self) -> None:
            return None

        async def receive(self) -> dict[str, object]:
            return {"type": "websocket.disconnect", "code": 1012}

        async def send_json(self, item: object) -> None:
            raise AssertionError("no event was published")

    async def run() -> None:
        await asyncio.wait_for(
            _stream(ClosedSocket(), Admitting(), event_bus),  # type: ignore[arg-type]
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


def test_no_credential_reaches_the_server_log_or_the_diagnostics(
    tmp_path: Path,
) -> None:
    """uvicorn logs every handshake with its path and query, and the app hands
    that logger to the diagnostics; only a real server writes those lines."""
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    event_bus = EventBus(store=store)
    logger = logging.getLogger("uvicorn.error")
    stderr = _Lines()
    level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(stderr)
    config = uvicorn.Config("", host="127.0.0.1", port=0, log_config=None)
    try:
        with config.bind_socket() as sock:
            port = sock.getsockname()[1]
            app = create_app(auth=ServerAuth(port), runtime=RuntimeStub(event_bus))
            config.app = app
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
                origin = base_url(port)
                token = owner_token(app)
                with httpx.Client(base_url=origin + API_PREFIX) as http:
                    link = http.post(
                        "/session/links",
                        headers={"Authorization": f"Bearer {token}"},
                    ).json()["url"]
                    code = link.split("#", 1)[1]
                    entered = http.post("/session", json={"code": code})
                    cookie = entered.cookies[app.state.auth.cookie]
                    csrf = entered.json()["csrf"]
                url = f"ws://127.0.0.1:{port}{API_PREFIX}/events"
                with connect(
                    url,
                    origin=origin,
                    additional_headers={"Cookie": f"{app.state.auth.cookie}={cookie}"},
                ) as socket:
                    selected = socket.subprotocol
                with (
                    pytest.raises(InvalidStatus) as rejected,
                    connect(url, origin=origin),
                ):
                    pass
            finally:
                server.should_exit = True
                thread.join(timeout=10)
    finally:
        logger.removeHandler(stderr)
        logger.setLevel(level)

    assert selected is None
    assert rejected.value.response.status_code == FORBIDDEN
    [session] = (tmp_path / "sessions").glob("system-*.jsonl")
    recorded = [json.loads(line) for line in session.read_text("utf-8").splitlines()]
    logged = {
        "stderr": "\n".join(stderr.lines),
        "diagnostics": "\n".join(item.get("message", "") for item in recorded),
    }
    # Both channels carried the handshakes, so their silence about the
    # credentials is not for want of lines.
    for channel, lines in logged.items():
        assert f'"WebSocket {API_PREFIX}/events" [accepted]' in lines, channel
        assert f'"WebSocket {API_PREFIX}/events" 403' in lines, channel
        for secret in (token, code, cookie, csrf):
            assert secret not in lines, channel
    # The server's own records are where the credentials are meant to be
    # (what they keep of them is ``test_auth.py``'s).
    for path in tmp_path.rglob("*"):
        if path.is_file() and not path.is_relative_to(server_directory(port)):
            text = path.read_text("utf-8")
            for secret in (token, code, cookie, csrf):
                assert secret not in text, path
