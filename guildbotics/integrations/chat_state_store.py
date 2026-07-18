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
class ThreadHandoffState:
    person_id: str
    roles: list[str] = field(default_factory=list)
    message_ts: str = ""
    text: str = ""
    thread_topic: str = ""
    latest_focus: str = ""


@dataclass(slots=True)
class ThreadSystemNoticeState:
    kind: str
    person_id: str
    source_event_id: str
    reason: str = "failed"
    message_ts: str = ""
    run_id: str = ""
    retry_after_at: str = ""
    retry_after_text: str = ""
    recorded_at: str = ""


@dataclass(slots=True)
class ThreadConversationState:
    channel_id: str
    thread_ts: str
    participants: set[str] = field(default_factory=set)  # person_id
    thread_topic: str = ""
    latest_focus: str = ""
    handoffs: list[ThreadHandoffState] = field(default_factory=list)
    system_notices: list[ThreadSystemNoticeState] = field(default_factory=list)
    backfill_disabled_reason: str = ""
    backfill_error_count: int = 0
    last_backfill_error: str = ""


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


@dataclass(slots=True)
class PendingChatEvent:
    """A queued chat event plus the participation policy of its channel.

    Participation comes from the channel subscription (resolved by the event
    listener) and is persisted so the consumer (the member worker) can rebuild
    the IncomingChatEvent without re-resolving subscriptions.
    """

    event: ChatEvent
    chat_participation: str = "strict"
    attempt_count: int = 0
    max_attempts: int = 5
    next_attempt_at: str | None = None
    run_id: str = ""
    last_error_category: str = ""
    wake_cursor: str = ""


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
    def list_thread_states(
        self, service: str, person_id: str, channel_id: str
    ) -> list[ThreadConversationState]:
        """List locally known thread states for a channel."""

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
    ) -> list[PendingChatEvent]:
        """Load durable unprocessed chat events (with participation) for a channel."""

    @abstractmethod
    def upsert_pending_event(
        self,
        service: str,
        person_id: str,
        channel_id: str,
        event: ChatEvent,
        chat_participation: str = "strict",
    ) -> None:
        """Durably store an unprocessed event (idempotent by event_id)."""

    @abstractmethod
    def save_pending_event(
        self,
        service: str,
        person_id: str,
        channel_id: str,
        pending: PendingChatEvent,
    ) -> None:
        """Persist an already loaded pending event, including retry state."""

    @abstractmethod
    def remove_pending_event(
        self, service: str, person_id: str, channel_id: str, event_id: str
    ) -> None:
        """Remove a durable pending event after successful processing."""

    @abstractmethod
    def list_pending_channels(self, person_id: str) -> list[tuple[str, str]]:
        """List (service, channel_id) pairs that currently hold pending events
        for a person, so a consumer can drain them without resolving
        subscriptions."""

    @abstractmethod
    def list_known_channels(self, service: str, person_id: str) -> list[str]:
        """List channel ids that hold any stored receive state (cursor, pending
        events, or tracked threads) for a person on a service."""

    @abstractmethod
    def load_receive_cutoff(self, service: str, person_id: str) -> str | None:
        """Load the receive cutoff ts, or None when no reset has been recorded.

        Backfill treats this as a hard floor: messages at or before it are never
        re-fetched, regardless of per-channel watermark or overlap. It applies to
        every subscribed channel, including ones only known by name that have no
        stored per-channel state yet.
        """

    @abstractmethod
    def save_receive_cutoff(self, service: str, person_id: str, cutoff_ts: str) -> None:
        """Persist the receive cutoff ts for a person on a service."""

    @abstractmethod
    def clear_channel_receive_backlog(
        self, service: str, person_id: str, channel_id: str
    ) -> None:
        """Drop received-but-unprocessed pending events and tracked threads for a
        channel, so a reset does not re-process a stale backlog. Does not start
        or stop any listener; the caller must ensure the runtime is stopped.
        """
