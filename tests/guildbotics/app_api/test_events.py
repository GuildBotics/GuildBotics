"""Unit tests for ``guildbotics.app_api.events``."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from guildbotics.app_api.events import (
    EventBus,
    EventBusLogHandler,
)
from guildbotics.observability import trace_scope

EXPECTED_HISTORY_LEN = 3


async def _get_with_timeout(subscription, timeout: float = 2.0) -> dict[str, object]:
    return await asyncio.wait_for(subscription.get(), timeout=timeout)


@pytest.mark.asyncio
async def test_event_bus_respects_history_limit() -> None:
    bus = EventBus(history_limit=3)
    for index in range(5):
        bus.publish_event("tick", {"index": index})

    snapshot = bus.snapshot_events()
    assert len(snapshot) == EXPECTED_HISTORY_LEN
    assert [item["payload"]["index"] for item in snapshot] == [2, 3, 4]


@pytest.mark.asyncio
async def test_subscribe_replays_existing_history() -> None:
    bus = EventBus()
    bus.publish_event("first", {"n": 1})
    bus.publish_event("second", {"n": 2})

    subscription = bus.subscribe_events()
    try:
        first = await _get_with_timeout(subscription)
        second = await _get_with_timeout(subscription)
    finally:
        subscription.close()

    assert first["type"] == "first"
    assert first["payload"] == {"n": 1}
    assert second["type"] == "second"
    assert second["payload"] == {"n": 2}


@pytest.mark.asyncio
async def test_no_publish_to_subscriber_after_close() -> None:
    bus = EventBus()
    subscription = bus.subscribe_events()
    subscription.close()

    bus.publish_event("after-close", {"n": 1})

    with pytest.raises(asyncio.TimeoutError):
        await _get_with_timeout(subscription, timeout=0.2)


@pytest.mark.asyncio
async def test_event_and_log_history_are_separate() -> None:
    bus = EventBus()
    bus.publish_event("command.started", {"command": "hello"})
    bus.publish_log("INFO", "log line")

    event_sub = bus.subscribe_events()
    log_sub = bus.subscribe_logs()
    try:
        event_item = await _get_with_timeout(event_sub)
        log_item = await _get_with_timeout(log_sub)

        # Event subscriber must not see the log entry and vice versa.
        with pytest.raises(asyncio.TimeoutError):
            await _get_with_timeout(event_sub, timeout=0.2)
        with pytest.raises(asyncio.TimeoutError):
            await _get_with_timeout(log_sub, timeout=0.2)
    finally:
        event_sub.close()
        log_sub.close()

    assert event_item["type"] == "command.started"
    assert "level" not in event_item
    assert log_item["level"] == "INFO"
    assert log_item["message"] == "log line"
    assert "type" not in log_item


@pytest.mark.asyncio
async def test_live_event_payload_shape_inside_trace() -> None:
    bus = EventBus()
    subscription = bus.subscribe_events()
    try:
        with trace_scope(
            "manual", command="hello", trace_id="trace-1", person_id="alice"
        ):
            bus.publish_event("command.done", {"ok": True})
        item = await _get_with_timeout(subscription)
    finally:
        subscription.close()

    assert item["kind"] == "event"
    assert item["type"] == "command.done"
    assert item["trace_id"] == "trace-1"
    assert item["source"] == "manual"
    assert item["command"] == "hello"
    assert item["person_id"] == "alice"
    assert item["payload"] == {"ok": True}
    assert item["timestamp"]


@pytest.mark.asyncio
async def test_publish_event_defaults_payload_to_empty_dict() -> None:
    bus = EventBus()
    subscription = bus.subscribe_events()
    try:
        bus.publish_event("bare")
        item = await _get_with_timeout(subscription)
    finally:
        subscription.close()

    assert item["payload"] == {}
    assert item["trace_id"] is None
    assert item["source"] is None


@pytest.mark.asyncio
async def test_publish_event_merges_source_and_attributes() -> None:
    bus = EventBus()
    subscription = bus.subscribe_events()
    try:
        bus.publish_event(
            "scheduler.running",
            {"state": "running"},
            source="scheduler",
            attributes={"service_run_id": "svc-1"},
        )
        item = await _get_with_timeout(subscription)
    finally:
        subscription.close()

    assert item["source"] == "scheduler"
    assert item["attributes"] == {"service_run_id": "svc-1"}


@pytest.mark.asyncio
async def test_background_thread_publish_reaches_async_subscriber() -> None:
    bus = EventBus()
    subscription = bus.subscribe_events()

    def publish_from_thread() -> None:
        bus.publish_event("from-thread", {"n": 42})

    thread = threading.Thread(target=publish_from_thread)
    try:
        thread.start()
        item = await _get_with_timeout(subscription)
    finally:
        thread.join(timeout=2.0)
        subscription.close()

    assert item["type"] == "from-thread"
    assert item["payload"] == {"n": 42}


@pytest.mark.asyncio
async def test_event_bus_log_handler_publishes_formatted_log() -> None:
    bus = EventBus()
    log_sub = bus.subscribe_logs()
    event_sub = bus.subscribe_events()

    logger = logging.getLogger("test_event_bus_log_handler")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = EventBusLogHandler(bus)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    try:
        logger.warning("disk almost full")
        item = await _get_with_timeout(log_sub)

        # A plain log handler must not emit on the event channel.
        with pytest.raises(asyncio.TimeoutError):
            await _get_with_timeout(event_sub, timeout=0.2)
    finally:
        logger.removeHandler(handler)
        log_sub.close()
        event_sub.close()

    assert item["kind"] == "log"
    assert item["level"] == "WARNING"
    assert item["message"] == "disk almost full"
    assert item["trace_id"] is None


@pytest.mark.asyncio
async def test_log_handler_attaches_current_trace_to_log_records() -> None:
    bus = EventBus()
    log_sub = bus.subscribe_logs()

    logger = logging.getLogger("test_log_handler_trace")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = EventBusLogHandler(bus)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    try:
        with trace_scope("manual", trace_id="trace-42"):
            logger.error("command failed")
        item = await _get_with_timeout(log_sub)
    finally:
        logger.removeHandler(handler)
        log_sub.close()

    # Logs emitted within a trace carry that trace id (the single log path that
    # replaced the old command.log events).
    assert item["kind"] == "log"
    assert item["level"] == "ERROR"
    assert item["message"] == "command failed"
    assert item["trace_id"] == "trace-42"
