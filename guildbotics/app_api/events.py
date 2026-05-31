from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any]]


class EventSubscription:
    def __init__(
        self,
        subscribers: set[_Subscriber],
        history: deque[dict[str, Any]],
    ) -> None:
        self._subscribers = subscribers
        self._subscriber = _Subscriber(asyncio.get_running_loop(), asyncio.Queue())
        for item in history:
            self._subscriber.queue.put_nowait(item)
        subscribers.add(self._subscriber)

    async def get(self) -> dict[str, Any]:
        return await self._subscriber.queue.get()

    def close(self) -> None:
        self._subscribers.discard(self._subscriber)


class EventBus:
    def __init__(self, history_limit: int = 200) -> None:
        self._history_limit = history_limit
        self._event_history: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self._log_history: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self._event_subscribers: set[_Subscriber] = set()
        self._log_subscribers: set[_Subscriber] = set()

    def publish_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> None:
        self._publish(
            self._event_history,
            self._event_subscribers,
            {
                "type": event_type,
                "request_id": request_id,
                "payload": payload or {},
                "timestamp": _timestamp(),
            },
        )

    def publish_log(
        self, level: str, message: str, *, request_id: str | None = None
    ) -> None:
        self._publish(
            self._log_history,
            self._log_subscribers,
            {
                "level": level,
                "message": message,
                "request_id": request_id,
                "timestamp": _timestamp(),
            },
        )

    def subscribe_events(self) -> EventSubscription:
        return EventSubscription(self._event_subscribers, self._event_history)

    def subscribe_logs(self) -> EventSubscription:
        return EventSubscription(self._log_subscribers, self._log_history)

    def snapshot_events(self) -> list[dict[str, Any]]:
        return list(self._event_history)

    def snapshot_logs(self) -> list[dict[str, Any]]:
        return list(self._log_history)

    def _publish(
        self,
        history: deque[dict[str, Any]],
        subscribers: set[_Subscriber],
        item: dict[str, Any],
    ) -> None:
        history.append(item)
        for subscriber in list(subscribers):
            subscriber.loop.call_soon_threadsafe(subscriber.queue.put_nowait, item)


class EventBusLogHandler(logging.Handler):
    def __init__(self, event_bus: EventBus) -> None:
        super().__init__()
        self._event_bus = event_bus

    def emit(self, record: logging.LogRecord) -> None:
        self._event_bus.publish_log(record.levelname, self.format(record))


class CommandEventLogHandler(logging.Handler):
    def __init__(self, event_bus: EventBus, request_id: str) -> None:
        super().__init__()
        self._event_bus = event_bus
        self._request_id = request_id

    def emit(self, record: logging.LogRecord) -> None:
        self._event_bus.publish_event(
            "command.log",
            {"level": record.levelname, "message": self.format(record)},
            request_id=self._request_id,
        )


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat()
