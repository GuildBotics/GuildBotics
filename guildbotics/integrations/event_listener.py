from __future__ import annotations

from abc import ABC, abstractmethod

from guildbotics.runtime.chat_service import ChatEvent


class EventListener(ABC):
    """The chat events one connection of a provider receives."""

    @property
    @abstractmethod
    def connected(self) -> bool:
        """Whether events can arrive now."""

    @property
    @abstractmethod
    def auth_failed(self) -> bool:
        """Whether the provider refused the connection's credential for good."""

    @abstractmethod
    def start(self) -> None:
        """Start background receiving."""

    @abstractmethod
    def stop(self) -> None:
        """Stop background receiving and release resources."""

    @abstractmethod
    def drain_events(self) -> list[ChatEvent]:
        """Drain the events queued since the last call, in channel order."""
