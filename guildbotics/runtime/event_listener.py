from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

from guildbotics.integrations.chat_service import ChatEvent


@dataclass(slots=True)
class IncomingChatEvent:
    service_name: str
    channel_id: str
    event: ChatEvent
    chat_participation: str = "strict"

    def to_shared_state(self) -> dict[str, object]:
        return {
            "service_name": self.service_name,
            "channel_id": self.channel_id,
            "event": asdict(self.event),
            "chat_participation": self.chat_participation,
        }

    @classmethod
    def from_shared_state(cls, value: object) -> IncomingChatEvent | None:
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
                chat_participation=str(value.get("chat_participation", "strict")),
            )
        except Exception:
            return None


def _parse_event_from_shared_state(
    service_name: str, raw_event: dict[str, object]
) -> ChatEvent | None:
    if service_name == "slack":
        return _parse_slack_event_from_shared_state(raw_event)
    return _parse_generic_event_from_shared_state(raw_event)


def _parse_slack_event_from_shared_state(
    raw_event: dict[str, object],
) -> ChatEvent | None:
    # Slack-specific parser entrypoint (currently same schema as generic ChatEvent).
    return _parse_generic_event_from_shared_state(raw_event)


def _parse_generic_event_from_shared_state(
    raw_event: dict[str, object],
) -> ChatEvent | None:
    try:
        event_id = str(raw_event["event_id"])
        channel_id = str(raw_event["channel_id"])
        message_ts = str(raw_event["message_ts"])
        thread_ts = str(raw_event["thread_ts"])
        author_id_raw = raw_event.get("author_id")
        mentions_raw = raw_event.get("mentions", [])
        mentions = (
            [str(item) for item in mentions_raw]
            if isinstance(mentions_raw, list)
            else []
        )
        metadata_raw = raw_event.get("metadata")
        metadata = (
            {str(key): item for key, item in metadata_raw.items() if str(key)}
            if isinstance(metadata_raw, dict)
            else {}
        )
        return ChatEvent(
            event_id=event_id,
            channel_id=channel_id,
            message_ts=message_ts,
            thread_ts=thread_ts,
            author_id=None if author_id_raw is None else str(author_id_raw),
            text=str(raw_event.get("text", "")),
            mentions=mentions,
            is_edit_or_delete=bool(raw_event.get("is_edit_or_delete", False)),
            is_bot_message=bool(raw_event.get("is_bot_message", False)),
            is_thread_reply=bool(raw_event.get("is_thread_reply", False)),
            metadata=metadata,
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
