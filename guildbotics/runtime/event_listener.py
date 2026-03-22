from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
import dataclasses

from guildbotics.integrations.chat_service import ChatEvent

INCOMING_CHAT_EVENT_KEY = "incoming_event"


@dataclass(slots=True)
class ChatSubscriptionEvent:
    service_name: str
    channel_id: str
    event: ChatEvent


@dataclass(slots=True)
class IncomingChatEvent:
    service_name: str
    channel_id: str
    event: ChatEvent

    def to_shared_state(self) -> dict[str, object]:
        return {
            "service_name": self.service_name,
            "channel_id": self.channel_id,
            "event": asdict(self.event),
        }

    @classmethod
    def from_shared_state(cls, value: object) -> "IncomingChatEvent | None":
        if not isinstance(value, dict):
            return None
        service_name = str(value.get("service_name", "slack"))
        raw_event = value.get("event")
        if not isinstance(raw_event, dict):
            return None
        try:
            event = _parse_event_from_shared_state(service_name, raw_event)
            if event is None:
                return None
            return cls(
                service_name=service_name,
                channel_id=str(value.get("channel_id") or event.channel_id),
                event=event,
            )
        except Exception:
            return None


def _parse_event_from_shared_state(
    service_name: str, raw_event: dict[str, object]
) -> ChatEvent | None:
    if service_name == "slack":
        return _parse_slack_event_from_shared_state(raw_event)
    return _parse_generic_event_from_shared_state(raw_event)


def _parse_slack_event_from_shared_state(raw_event: dict[str, object]) -> ChatEvent | None:
    # Slack-specific parser entrypoint (currently same schema as generic ChatEvent).
    return _parse_generic_event_from_shared_state(raw_event)


def _parse_generic_event_from_shared_state(raw_event: dict[str, object]) -> ChatEvent | None:
    event_fields = {field.name for field in dataclasses.fields(ChatEvent)}
    normalized_raw_event = {
        key: val for key, val in raw_event.items() if key in event_fields
    }
    try:
        return ChatEvent(**normalized_raw_event)
    except Exception:
        return None


class EventListener(ABC):
    @abstractmethod
    def start(self) -> None:
        """Start background receiving."""

    @abstractmethod
    def stop(self) -> None:
        """Stop background receiving and release resources."""

    @abstractmethod
    def drain_events(self) -> list[IncomingChatEvent]:
        """Drain queued events collected since the last call."""
