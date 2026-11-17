"""The chat port: a member's chat, whichever provider ``project.services``
names for ``chat_service``.

Messages are named by opaque ids and ordered by ``message_position``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict

from guildbotics.runtime.port import SemanticReaction, WorkflowStatus


def message_position(occurred_at: datetime, message_id: str) -> str:
    """Where a message stands in its channel's order, as comparable text:
    the UTC time at fixed width, then the id for messages of the same time."""
    return f"{occurred_at.astimezone(UTC):%Y-%m-%dT%H:%M:%S.%fZ} {message_id}"


class ChatIdentityOutput(TypedDict):
    """The member CLI identity output read by the guest."""

    service: str
    user_id: str
    display_name: str
    person_id: str


class ChatPostOutput(TypedDict):
    """The member CLI post/reply output read by the guest."""

    service: str
    channel_id: str
    message_id: str
    thread_id: str
    occurred_at: str
    text: str
    posted: bool


class ChatReactionOutput(TypedDict):
    """The member CLI reaction output read by the guest."""

    service: str
    channel_id: str
    message_id: str
    reaction: SemanticReaction
    reacted: bool


class ChatChannelOutput(TypedDict):
    """The member CLI channel lookup output read by the guest."""

    service: str
    channel_name: str
    channel_id: str


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
