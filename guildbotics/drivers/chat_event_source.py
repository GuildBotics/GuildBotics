from __future__ import annotations

"""Polling-oriented event source abstractions and compatibility wrappers.

Socket Mode runtime execution has moved to `EventListenerRunner` + `EventListener`.
This module remains for polling-based flows and compatibility wrappers.
"""

from abc import ABC, abstractmethod
from typing import Any

from guildbotics.integrations.chat_service import ChatService
from guildbotics.integrations.chat_state_store import ConversationStateStore
from guildbotics.runtime.event_listener import ChatSubscriptionEvent


class ChatEventSource(ABC):
    @abstractmethod
    async def fetch_events(
        self, *, person_id: str, subscriptions: list[dict[str, Any]]
    ) -> list[ChatSubscriptionEvent]:
        """Fetch new events for the current scheduler cycle."""

    @abstractmethod
    def mark_processed(self, *, person_id: str, item: ChatSubscriptionEvent) -> None:
        """Record that the workflow successfully processed an event."""

    @abstractmethod
    def finalize_cycle(self, *, person_id: str) -> None:
        """Persist cursor/offset updates for the current scheduler cycle."""


class PollingChatEventSource(ChatEventSource):
    """ChatEventSource backed by ChatService.list_channel_events polling."""

    def __init__(
        self,
        *,
        chat_service: ChatService,
        state_store: ConversationStateStore,
    ) -> None:
        self._chat_service = chat_service
        self._state_store = state_store
        self._pending: dict[
            tuple[str, str, str],  # (person_id, service, channel)
            tuple[str | None, str | None, list[str]],  # (cursor, oldest_ts, processed_ids)
        ] = {}

    async def fetch_events(
        self, *, person_id: str, subscriptions: list[dict[str, Any]]
    ) -> list[ChatSubscriptionEvent]:
        self._pending = {}
        out: list[ChatSubscriptionEvent] = []
        for sub in subscriptions:
            if not bool(sub.get("enabled", True)):
                continue

            service_name = str(sub.get("service", "slack")).lower()
            channel_id = str(sub.get("channel_id", "")).strip()
            if not channel_id:
                continue

            cursor_state = self._state_store.load_channel_cursor(
                service_name, person_id, channel_id
            )
            page = await self._chat_service.list_channel_events(
                channel_id, cursor=cursor_state.cursor, oldest_ts=cursor_state.oldest_ts
            )

            key = (person_id, service_name, channel_id)
            self._pending[key] = (
                page.cursor if page.cursor is not None else cursor_state.cursor,
                _max_ts(cursor_state.oldest_ts, page.oldest_ts),
                list(cursor_state.processed_event_ids),
            )

            for event in page.events:
                out.append(
                    ChatSubscriptionEvent(
                        service_name=service_name,
                        channel_id=channel_id,
                        event=event,
                    )
                )
        return out

    def mark_processed(self, *, person_id: str, item: ChatSubscriptionEvent) -> None:
        key = (person_id, item.service_name, item.channel_id)
        pending = self._pending.get(key)
        if pending is None:
            return
        cursor, oldest_ts, processed_event_ids = pending
        processed_event_ids.append(item.event.event_id)
        self._pending[key] = (
            cursor,
            _max_ts(oldest_ts, item.event.message_ts),
            processed_event_ids,
        )

    def finalize_cycle(self, *, person_id: str) -> None:
        for (pending_person_id, service_name, channel_id), (
            cursor,
            oldest_ts,
            processed_event_ids,
        ) in self._pending.items():
            if pending_person_id != person_id:
                continue
            state = self._state_store.load_channel_cursor(service_name, person_id, channel_id)
            state.cursor = cursor
            state.oldest_ts = oldest_ts
            state.processed_event_ids = _dedupe_keep_order(
                list(state.processed_event_ids) + list(processed_event_ids)
            )
            self._state_store.save_channel_cursor(service_name, person_id, channel_id, state)
        self._pending = {}


def _max_ts(a: str | None, b: str | None) -> str | None:
    if not a:
        return b
    if not b:
        return a
    try:
        return a if float(a) >= float(b) else b
    except ValueError:
        return a if a >= b else b


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
