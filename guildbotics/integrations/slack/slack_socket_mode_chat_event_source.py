from __future__ import annotations

import asyncio
from logging import Logger
from typing import Any, Callable

import httpx

from guildbotics.drivers.chat_event_source import ChatEventSource
from guildbotics.integrations.chat_state_store import ConversationStateStore
from guildbotics.integrations.slack.slack_socket_listener import SlackSocketEventListener
from guildbotics.runtime.event_listener import ChatSubscriptionEvent


class SocketModeChatEventSource(ChatEventSource):
    """Compatibility wrapper over SlackSocketEventListener for ChatEventSource callers.

    Removal conditions:
    - no production code depends on Socket Mode via `ChatEventSource`
    - compatibility tests are moved to `SlackSocketEventListener` / `EventListenerRunner`
    - Socket Mode polling-style compatibility path is no longer needed
    """

    def __init__(
        self,
        *,
        logger: Logger,
        person: Any,
        state_store: ConversationStateStore,
        base_url: str | None = None,
        max_events_per_cycle: int = 100,
        http_client: httpx.Client | None = None,
        ws_connect: Callable[[str], Any] | None = None,
    ) -> None:
        self._logger = logger
        self._person = person
        self._state_store = state_store
        self._max_events_per_cycle = max(1, int(max_events_per_cycle))
        self._listener = SlackSocketEventListener(
            logger=logger,
            app_token=self._require_app_token(person),
            base_url=base_url,
            http_client=http_client,
            ws_connect=ws_connect,
        )

    async def fetch_events(
        self, *, person_id: str, subscriptions: list[dict[str, Any]]
    ) -> list[ChatSubscriptionEvent]:
        subscribed_channels = {
            str(sub.get("channel_id", "")).strip()
            for sub in subscriptions
            if bool(sub.get("enabled", True))
            and str(sub.get("service", "slack")).lower() == "slack"
            and str(sub.get("channel_id", "")).strip()
        }
        if not subscribed_channels:
            return []

        self._listener.start()
        # Allow the reader thread to connect and queue initial frames.
        await asyncio.sleep(0.05)
        for incoming in self._listener.drain_events():
            self._state_store.upsert_pending_event(
                incoming.service_name,
                person_id,
                incoming.channel_id,
                incoming.event,
            )
        return self._load_pending_events(person_id, subscribed_channels)

    def mark_processed(self, *, person_id: str, item: ChatSubscriptionEvent) -> None:
        self._state_store.mark_processed_event(
            item.service_name, person_id, item.channel_id, item.event.event_id
        )
        self._state_store.remove_pending_event(
            item.service_name, person_id, item.channel_id, item.event.event_id
        )

    def finalize_cycle(self, *, person_id: str) -> None:
        return None

    async def aclose(self) -> None:
        self._listener.stop()

    def _load_pending_events(
        self, person_id: str, subscribed_channels: set[str]
    ) -> list[ChatSubscriptionEvent]:
        out: list[ChatSubscriptionEvent] = []
        for channel_id in sorted(subscribed_channels):
            events = self._state_store.load_pending_events("slack", person_id, channel_id)
            for event in events:
                out.append(
                    ChatSubscriptionEvent(
                        service_name="slack",
                        channel_id=channel_id,
                        event=event,
                    )
                )
                if len(out) >= self._max_events_per_cycle:
                    break
            if len(out) >= self._max_events_per_cycle:
                break
        out.sort(key=lambda item: item.event.message_ts)
        return out[: self._max_events_per_cycle]

    @staticmethod
    def _require_app_token(person: Any) -> str:
        if not getattr(person, "has_secret", None) or not person.has_secret("SLACK_APP_TOKEN"):
            env_key = person.to_person_env_key("SLACK_APP_TOKEN")
            raise ValueError(
                f"Slack App Token is required for Socket Mode on person '{person.person_id}'. "
                f"Set environment variable '{env_key}'."
            )
        return person.get_secret("SLACK_APP_TOKEN")
