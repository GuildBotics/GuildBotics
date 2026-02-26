from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from guildbotics.integrations.chat_service import ChatEvent

@dataclass(slots=True)
class ChannelCursorState:
    cursor: str | None = None
    oldest_ts: str | None = None
    processed_event_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ThreadConversationState:
    channel_id: str
    thread_ts: str
    participants: set[str] = field(default_factory=set)  # person_id
    last_bot_replier_id: str | None = None  # person_id
    response_expected: bool = True
    thread_claimed_by_other: bool = False


@dataclass(slots=True)
class ThreadMessageState:
    channel_id: str
    thread_ts: str
    message_ts: str
    author_id: str | None
    text: str
    mentions: list[str] = field(default_factory=list)
    is_bot_message: bool = False


@dataclass(slots=True)
class ScheduledPostState:
    last_run_slot: str | None = None


class ConversationStateStore(ABC):
    """Persistent store for chat polling cursors and per-thread state."""

    @abstractmethod
    def load_channel_cursor(
        self, service: str, person_id: str, channel_id: str
    ) -> ChannelCursorState:
        """Load channel polling cursor and processed event ids."""

    @abstractmethod
    def save_channel_cursor(
        self,
        service: str,
        person_id: str,
        channel_id: str,
        state: ChannelCursorState,
    ) -> None:
        """Persist channel polling cursor and processed event ids."""

    @abstractmethod
    def is_processed_event(
        self, service: str, person_id: str, channel_id: str, event_id: str
    ) -> bool:
        """Return True when the event_id is already recorded as processed."""

    @abstractmethod
    def mark_processed_event(
        self, service: str, person_id: str, channel_id: str, event_id: str
    ) -> None:
        """Record an event as processed."""

    @abstractmethod
    def load_thread_state(
        self, service: str, person_id: str, channel_id: str, thread_ts: str
    ) -> ThreadConversationState:
        """Load per-thread conversation state."""

    @abstractmethod
    def save_thread_state(
        self,
        service: str,
        person_id: str,
        channel_id: str,
        thread_ts: str,
        state: ThreadConversationState,
    ) -> None:
        """Persist per-thread conversation state."""

    @abstractmethod
    def load_thread_messages(
        self, service: str, person_id: str, channel_id: str, thread_ts: str
    ) -> list[ThreadMessageState]:
        """Load locally cached messages observed in a thread."""

    @abstractmethod
    def append_thread_message(
        self,
        service: str,
        person_id: str,
        channel_id: str,
        thread_ts: str,
        message: ThreadMessageState,
    ) -> None:
        """Append or update a locally cached thread message."""

    @abstractmethod
    def load_scheduled_post_state(
        self, service: str, person_id: str, schedule_name: str
    ) -> ScheduledPostState:
        """Load state for a scheduled chat post definition."""

    @abstractmethod
    def save_scheduled_post_state(
        self,
        service: str,
        person_id: str,
        schedule_name: str,
        state: ScheduledPostState,
    ) -> None:
        """Persist state for a scheduled chat post definition."""

    @abstractmethod
    def load_pending_events(
        self, service: str, person_id: str, channel_id: str
    ) -> list[ChatEvent]:
        """Load durable unprocessed chat events for a channel."""

    @abstractmethod
    def upsert_pending_event(
        self, service: str, person_id: str, channel_id: str, event: ChatEvent
    ) -> None:
        """Durably store an unprocessed event (idempotent by event_id)."""

    @abstractmethod
    def remove_pending_event(
        self, service: str, person_id: str, channel_id: str, event_id: str
    ) -> None:
        """Remove a durable pending event after successful processing."""
