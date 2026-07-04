from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

SemanticReaction = Literal["ack", "agree", "celebrate", "support"]
SEMANTIC_REACTIONS: tuple[SemanticReaction, ...] = (
    "ack",
    "agree",
    "celebrate",
    "support",
)


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
    is_edit_or_delete: bool = False
    is_bot_message: bool = False
    is_thread_reply: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

    def is_from_user(self, user_id: str | None) -> bool:
        if not user_id:
            return False
        return self.author_id == user_id


@dataclass(slots=True)
class ChatEventPage:
    events: list[ChatEvent] = field(default_factory=list)
    cursor: str | None = None
    oldest_ts: str | None = None


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
        latest_ts: str | None = None,
        limit: int = 100,
    ) -> ChatEventPage:
        """Fetch channel events incrementally."""

    @abstractmethod
    async def list_thread_events(
        self,
        channel_id: str,
        *,
        thread_ts: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ChatEventPage:
        """Fetch events in a single thread."""

    @abstractmethod
    async def resolve_channel_id(self, channel_name: str) -> str | None:
        """Resolve a human-friendly channel name to a stable channel_id."""

    @abstractmethod
    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatPostResult:
        """Post a message, optionally as a thread reply."""

    @abstractmethod
    async def add_reaction(
        self, channel_id: str, message_ts: str, reaction: str
    ) -> None:
        """Add a semantic reaction to a message."""

    def normalize_participant_text(
        self, text: str, participant_labels: dict[str, str]
    ) -> str:
        """Normalize service-specific participant syntax into workflow-friendly labels."""
        return text

    def render_participant_text(
        self, text: str, participant_labels: dict[str, str]
    ) -> str:
        """Render workflow-friendly participant labels into service-specific syntax."""
        return text
