"""Integration tests for the `/events` and `/logs` websocket endpoints.

These tests exercise the real endpoint wiring in
``guildbotics.app_api.api.create_app`` by driving the shared ``EventBus`` the
same way the application does (publishing events/logs through the bus that is
injected into the app). They assert concrete message payloads, the
policy-violation close on a bad token, replayed history content, delivery to an
already-connected client, and subscription cleanup after disconnect.
"""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.observability import trace_scope

POLICY_VIOLATION_CLOSE_CODE = 1008


class RuntimeStub:
    """Minimal runtime stub: websocket endpoints never touch it."""

    def stop_scheduler(self, *, force: bool = False) -> None:
        return None


def _app(event_bus: EventBus):
    return create_app(
        session_token="secret",
        runtime=RuntimeStub(),
        event_bus=event_bus,
    )


def test_events_success_receives_published_event(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)

    with (
        TestClient(app) as client,
        client.websocket_connect("/events?token=secret") as websocket,
    ):
        with trace_scope("manual", trace_id="request-live"):
            event_bus.publish_event(
                "command.started",
                {"command": "hello"},
            )
        event = websocket.receive_json()

    assert event["type"] == "command.started"
    assert event["trace_id"] == "request-live"
    assert event["payload"] == {"command": "hello"}
    assert event["timestamp"]


def test_events_invalid_token_closes_with_policy_violation(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/events?token=wrong") as websocket,
    ):
        websocket.receive_json()

    assert exc_info.value.code == POLICY_VIOLATION_CLOSE_CODE
    # No subscriber was ever registered for a rejected connection.
    assert event_bus._event_subscribers == set()


def test_logs_success_receives_published_log(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)

    with (
        TestClient(app) as client,
        client.websocket_connect("/logs?token=secret") as websocket,
    ):
        with trace_scope("manual", trace_id="log-live"):
            event_bus.publish_log("INFO", "scheduler started")
        log = websocket.receive_json()

    assert log["level"] == "INFO"
    assert log["message"] == "scheduler started"
    assert log["trace_id"] == "log-live"
    assert log["timestamp"]


def test_logs_invalid_token_closes_with_policy_violation(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/logs?token=wrong") as websocket,
    ):
        websocket.receive_json()

    assert exc_info.value.code == POLICY_VIOLATION_CLOSE_CODE
    assert event_bus._log_subscribers == set()


def test_events_history_is_replayed_on_connect(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)
    # Publish before any client connects: the item must live in history.
    with trace_scope("manual", trace_id="request-history"):
        event_bus.publish_event(
            "command.finished",
            {"output": "done"},
        )

    with (
        TestClient(app) as client,
        client.websocket_connect("/events?token=secret") as websocket,
    ):
        replayed = websocket.receive_json()

    assert replayed["type"] == "command.finished"
    assert replayed["trace_id"] == "request-history"
    assert replayed["payload"] == {"output": "done"}


def test_logs_history_is_replayed_on_connect(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)
    with trace_scope("manual", trace_id="log-history"):
        event_bus.publish_log("WARNING", "old log line")

    with (
        TestClient(app) as client,
        client.websocket_connect("/logs?token=secret") as websocket,
    ):
        replayed = websocket.receive_json()

    assert replayed["level"] == "WARNING"
    assert replayed["message"] == "old log line"
    assert replayed["trace_id"] == "log-history"


def test_events_history_then_live_delivery_in_order(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)
    with trace_scope("manual", trace_id="r1"):
        event_bus.publish_event("command.started", {"command": "a"})

    with (
        TestClient(app) as client,
        client.websocket_connect("/events?token=secret") as websocket,
    ):
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
        with client.websocket_connect("/events?token=secret") as websocket:
            event_bus.publish_event("command.started", {})
            websocket.receive_json()
            assert len(event_bus._event_subscribers) == 1

        # After the context manager exits the client has disconnected; the
        # endpoint's ``finally: queue.close()`` must drop the subscriber.
        _wait_for_no_subscribers(event_bus._event_subscribers)

    assert event_bus._event_subscribers == set()
    # A further publish after disconnect must not raise.
    event_bus.publish_event("command.finished", {})


def test_disconnect_closes_log_subscription(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = _app(event_bus)

    with TestClient(app) as client:
        with client.websocket_connect("/logs?token=secret") as websocket:
            event_bus.publish_log("INFO", "hi")
            websocket.receive_json()
            assert len(event_bus._log_subscribers) == 1

        _wait_for_no_subscribers(event_bus._log_subscribers)

    assert event_bus._log_subscribers == set()
    event_bus.publish_log("INFO", "after disconnect")


def _wait_for_no_subscribers(subscribers: set, timeout: float = 2.0) -> None:
    """Poll until the endpoint thread has run its cleanup, bounded by timeout."""
    deadline = time.monotonic() + timeout
    while subscribers and time.monotonic() < deadline:
        time.sleep(0.01)
