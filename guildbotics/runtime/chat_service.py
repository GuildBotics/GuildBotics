"""The chat port: a member's chat, whichever provider ``project.services``
names for ``chat_service``.

A message is named by opaque ids and placed in time by ``occurred_at``. Its
:attr:`ChatMessage.position` orders the messages of a channel as text, so the
same order holds wherever it is stored or sent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from guildbotics.entities.team import Person
from guildbotics.runtime.port import SemanticReaction, WorkflowStatus


def message_position(occurred_at: datetime, message_id: str) -> str:
    """Where a message stands in its channel's order, as comparable text:
    the UTC time at fixed width, then the id for messages of the same time."""
    return f"{occurred_at.astimezone(UTC):%Y-%m-%dT%H:%M:%S.%fZ} {message_id}"


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


@dataclass(slots=True, kw_only=True)
class ChatMessage:
    """A message of a thread, as the thread's history keeps it."""

    channel_id: str
    message_id: str
    thread_id: str
    occurred_at: datetime
    author_id: str | None
    text: str
    mentions: list[str] = field(default_factory=list)
    is_bot_message: bool = False

    @property
    def position(self) -> str:
        return message_position(self.occurred_at, self.message_id)

    def is_from_user(self, user_id: str | None) -> bool:
        if not user_id:
            return False
        return self.author_id == user_id


@dataclass(slots=True, kw_only=True)
class ChatEvent(ChatMessage):
    """A message as the work it may be for a member: ``event_id`` names it
    where it is queued and marked processed."""

    event_id: str
    is_thread_reply: bool = False
    #: The workflow status the message carries, which makes it no work.
    status: WorkflowStatus | None = None

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
    """A member's chat. Its operations fail as the other ports' do
    (:class:`~guildbotics.runtime.port.ServiceError`)."""

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
            ServiceNotFound: If the thread is not there.
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
        status: WorkflowStatus | None = None,
    ) -> ChatPostResult:
        """Post a message, optionally as a thread reply, carrying ``status``."""

    @abstractmethod
    async def add_reaction(
        self, channel_id: str, message_id: str, reaction: SemanticReaction
    ) -> None:
        """Add a semantic reaction to a message; a chat that has no reaction
        for its meaning raises ``ServiceUnsupported``."""

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
