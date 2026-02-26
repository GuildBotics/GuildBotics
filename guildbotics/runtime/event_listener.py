from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

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
        raw_event = value.get("event")
        if not isinstance(raw_event, dict):
            return None
        try:
            event = ChatEvent(**raw_event)
            return cls(
                service_name=str(value.get("service_name", "slack")),
                channel_id=str(value.get("channel_id") or event.channel_id),
                event=event,
            )
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

