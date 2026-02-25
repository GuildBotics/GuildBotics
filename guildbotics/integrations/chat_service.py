from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(slots=True)
class ChatIdentity:
    user_id: str
    display_name: str = ""


@dataclass(slots=True)
class ChatEvent:
    event_id: str
    channel_id: str
    message_ts: str
    thread_ts: str
    author_id: str | None
    text: str
    mentions: list[str] = field(default_factory=list)
    is_message: bool = True
    is_edit_or_delete: bool = False
    is_bot_message: bool = False
    is_thread_reply: bool = False


@dataclass(slots=True)
class ChatEventPage:
    events: list[ChatEvent] = field(default_factory=list)
    cursor: str | None = None
    oldest_ts: str | None = None


@dataclass(slots=True)
class ChatMessage:
    channel_id: str
    message_ts: str
    thread_ts: str
    author_id: str | None
    text: str
    mentions: list[str] = field(default_factory=list)
    is_bot_message: bool = False


@dataclass(slots=True)
class ChatPostResult:
    channel_id: str
    message_ts: str
    thread_ts: str


class ChatService(ABC):
    @abstractmethod
    async def get_bot_identity(self) -> ChatIdentity:
        """Return identity for the current bot/app user."""

    @abstractmethod
    async def list_channel_events(
        self,
        channel_id: str,
        *,
        cursor: str | None = None,
        oldest_ts: str | None = None,
        limit: int = 100,
    ) -> ChatEventPage:
        """Fetch channel events incrementally."""

    @abstractmethod
    async def resolve_channel_id(self, channel_name: str) -> str | None:
        """Resolve a human-friendly channel name to a stable channel_id."""

    @abstractmethod
    async def post_message(
        self, channel_id: str, text: str, *, thread_ts: str | None = None
    ) -> ChatPostResult:
        """Post a message, optionally as a thread reply."""

    @abstractmethod
    async def add_reaction(
        self, channel_id: str, message_ts: str, reaction: str
    ) -> None:
        """Add a reaction to a message."""
