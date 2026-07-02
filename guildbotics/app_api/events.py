from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from guildbotics.observability import correlation_fields

if TYPE_CHECKING:
    from guildbotics.observability.diagnostics_store import DiagnosticsStore


@dataclass(frozen=True)
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any]]


class EventSubscription:
    def __init__(
        self,
        subscribers: set[_Subscriber],
        history: deque[dict[str, Any]],
        lock: threading.Lock,
    ) -> None:
        self._subscribers = subscribers
        self._lock = lock
        self._subscriber = _Subscriber(asyncio.get_running_loop(), asyncio.Queue())
        with lock:
            history_items = list(history)
            subscribers.add(self._subscriber)
        for item in history_items:
            self._subscriber.queue.put_nowait(item)

    async def get(self) -> dict[str, Any]:
        return await self._subscriber.queue.get()

    def close(self) -> None:
        with self._lock:
            self._subscribers.discard(self._subscriber)


class EventBus:
    def __init__(
        self,
        history_limit: int = 200,
        *,
        store: DiagnosticsStore | None = None,
    ) -> None:
        self._history_limit = history_limit
        self._event_history: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self._log_history: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self._event_subscribers: set[_Subscriber] = set()
        self._log_subscribers: set[_Subscriber] = set()
        self._lock = threading.Lock()
        self._store = store

    def publish_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        source: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        correlation = correlation_fields()
        item = {
            "kind": "event",
            "type": event_type,
            **_correlation_record(correlation, source=source, attributes=attributes),
            "payload": payload or {},
            "timestamp": _timestamp(),
        }
        self._publish(self._event_history, self._event_subscribers, item)

    def publish_log(self, level: str, message: str) -> None:
        correlation = correlation_fields()
        item = {
            "kind": "log",
            "level": level,
            "message": message,
            **_correlation_record(correlation),
            "timestamp": _timestamp(),
        }
        self._publish(self._log_history, self._log_subscribers, item)

    def subscribe_events(self) -> EventSubscription:
        return EventSubscription(
            self._event_subscribers, self._event_history, self._lock
        )

    def subscribe_logs(self) -> EventSubscription:
        return EventSubscription(self._log_subscribers, self._log_history, self._lock)

    def snapshot_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._event_history)

    def _publish(
        self,
        history: deque[dict[str, Any]],
        subscribers: set[_Subscriber],
        item: dict[str, Any],
    ) -> None:
        with self._lock:
            history.append(item)
            current_subscribers = list(subscribers)
        for subscriber in current_subscribers:
            subscriber.loop.call_soon_threadsafe(subscriber.queue.put_nowait, item)
        if self._store is not None:
            self._store.record(item)


class EventBusLogHandler(logging.Handler):
    def __init__(self, event_bus: EventBus) -> None:
        super().__init__()
        self._event_bus = event_bus

    def emit(self, record: logging.LogRecord) -> None:
        self._event_bus.publish_log(record.levelname, self.format(record))


def _correlation_record(
    correlation: dict[str, Any],
    *,
    source: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged_attributes = dict(correlation.get("attributes") or {})
    if attributes:
        merged_attributes.update(attributes)
    record = {
        "trace_id": correlation.get("trace_id"),
        "span_id": correlation.get("span_id"),
        "parent_id": correlation.get("parent_id"),
        "span": correlation.get("span", ""),
        "source": source or correlation.get("source"),
        "person_id": correlation.get("person_id", ""),
        "command": correlation.get("command", ""),
        "workflow": correlation.get("workflow", ""),
        "attributes": merged_attributes,
    }
    return record


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat()
