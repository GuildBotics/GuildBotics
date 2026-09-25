from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from guildbotics.integrations.chat_service import ChatEvent


@dataclass(slots=True)
class IncomingChatEvent:
    service_name: str
    channel_id: str
    event: ChatEvent
    chat_participation: str = "strict"


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
