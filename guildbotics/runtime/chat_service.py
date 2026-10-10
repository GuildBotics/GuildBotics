"""The chat port: a member's chat, whichever provider ``project.services``
names for ``chat_service``.

A message is named by opaque ids and placed in time by ``occurred_at``. Its
:attr:`ChatEvent.position` orders the messages of a channel as text, so the
same order holds wherever it is stored or sent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from guildbotics.entities.team import Person

SemanticReaction = Literal["ack", "agree", "celebrate", "support"]
SEMANTIC_REACTIONS: tuple[SemanticReaction, ...] = (
    "ack",
    "agree",
    "celebrate",
    "support",
)


def message_position(occurred_at: datetime, message_id: str) -> str:
    """Where a message stands in its channel's order, as comparable text:
    the UTC time at fixed width, then the id for messages of the same time."""
    return f"{occurred_at.astimezone(UTC):%Y-%m-%dT%H:%M:%S.%fZ} {message_id}"


class ChatServiceError(RuntimeError):
    """An anticipated chat operation failure with a message fit for the user."""


class ChatCredentialsError(ChatServiceError):
    """The member has no credential the operation needs."""


class ChatThreadNotFoundError(ChatServiceError):
    """The thread asked for is not there (any more)."""


@dataclass(slots=True)
class ChatIdentity:
    user_id: str
    display_name: str = ""
    # Human-readable name of the workspace the credential belongs to, so the
    # setup GUI can show which workspace a pasted token actually reaches.
    workspace: str = ""


@dataclass(slots=True)
class CredentialCheck:
    """Whether one credential of a member's chat is configured and works."""

    name: str
    status: Literal["ok", "failed", "unconfigured"]
    error: str = ""


@dataclass(slots=True)
class ChatMessageRef:
    """The message a message URL names, and the thread it is in."""

    channel_id: str
    message_id: str
    thread_id: str


@dataclass(slots=True)
class ChatEvent:
    event_id: str
    channel_id: str
    message_id: str
    thread_id: str
    occurred_at: datetime
    author_id: str | None
    text: str
    mentions: list[str] = field(default_factory=list)
    is_edit_or_delete: bool = False
    is_bot_message: bool = False
    is_thread_reply: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def position(self) -> str:
        return message_position(self.occurred_at, self.message_id)

    def payload(self) -> dict[str, Any]:
        """The event as the member commands print it."""
        return {
            "event_id": self.event_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "occurred_at": self.occurred_at.isoformat(),
            "author_id": self.author_id or "",
            "text": self.text,
            "mentions": self.mentions,
            "is_bot_message": self.is_bot_message,
            "is_thread_reply": self.is_thread_reply,
        }

    def is_from_user(self, user_id: str | None) -> bool:
        if not user_id:
            return False
        return self.author_id == user_id


@dataclass(slots=True)
class ChatEventPage:
    events: list[ChatEvent] = field(default_factory=list)
    cursor: str | None = None


@dataclass(slots=True)
class ChatPostResult:
    channel_id: str
    message_id: str
    thread_id: str
    occurred_at: datetime


class ChatService(ABC):
    """A member's chat. Provider failures raise :class:`ChatServiceError`."""

    @abstractmethod
    async def get_bot_identity(self) -> ChatIdentity:
        """Return identity for the current bot/app user."""

    @abstractmethod
    async def check_credentials(self) -> list[CredentialCheck]:
        """Try each credential the member's chat uses, without writing."""

    @abstractmethod
    def self_user_id(self, person: Person) -> str:
        """The user ``person`` is on this chat as configured, or ``""``."""

    @abstractmethod
    def parse_message_url(self, url: str) -> ChatMessageRef:
        """The message a URL of this chat names."""

    @abstractmethod
    def mentioned_user_ids(self, text: str) -> list[str]:
        """The users ``text`` mentions, in this chat's syntax, once each."""

    @abstractmethod
    async def list_channel_events(
        self,
        channel_id: str,
        *,
        cursor: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> ChatEventPage:
        """Fetch the channel's messages that occurred in ``[since, until]``."""

    @abstractmethod
    async def list_thread_events(
        self,
        channel_id: str,
        *,
        thread_id: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ChatEventPage:
        """Fetch the messages of one thread.

        Raises:
            ChatThreadNotFoundError: If the thread is not there.
        """

    @abstractmethod
    async def resolve_channel_id(self, channel_name: str) -> str | None:
        """Resolve a human-friendly channel name to a stable channel_id."""

    @abstractmethod
    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatPostResult:
        """Post a message, optionally as a thread reply."""

    @abstractmethod
    async def add_reaction(
        self, channel_id: str, message_id: str, reaction: str
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
