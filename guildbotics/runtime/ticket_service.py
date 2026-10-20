"""The tickets a member works on and the board they are taken from.

A ticket is named by its URL. It is kept in a tracker (a repository on
GitHub, a project on Jira) and may belong to a repository of the code host;
the board knows which tickets are in which lane and whose they are.

Every operation fails as the code host's do
(:mod:`~guildbotics.runtime.code_hosting_service`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime
from logging import Logger
from typing import Any, Literal

from guildbotics.entities import Person, Task, Team
from guildbotics.runtime.port import SemanticReaction, WorkflowStatus
from guildbotics.runtime.resources import (
    CommentWritten,
    ReactionAdded,
    ReadModel,
    ReadPage,
    WorkKind,
    WorkTarget,
)

#: A lane a ticket is moved to.
MoveLane = Literal["ready", "in_progress", "done"]


class TicketCreated(ReadModel):
    labels: list[str]
    target: WorkTarget


class TicketUpdated(ReadModel):
    body: str
    state: str
    state_changed: bool
    labels: list[str]
    target: WorkTarget


class ClosedItem(ReadModel):
    """A ticket of the board, or a pull request of its work, that was closed
    (or merged)."""

    kind: WorkKind
    url: str
    title: str
    label: str
    #: When it was merged if it was, else when it was closed (ISO 8601).
    closed_at: str
    merged: bool


class TicketService(ABC):
    def __init__(self, logger: Logger, person: Person, team: Team):
        self.logger = logger
        self.person = person
        self.team = team

    @abstractmethod
    async def read(
        self,
        resource: str,
        subject: str,
        *,
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> ReadPage:
        """Read a ticket's resource of :data:`~.resources.TICKET_RESOURCES`,
        ``subject`` being the ticket's URL (see
        :meth:`~.code_hosting_service.CodeHostingService.read`)."""

    @abstractmethod
    async def comment(
        self, url: str, body: str, *, status: WorkflowStatus | None = None
    ) -> CommentWritten:
        """Comment on the ticket ``url``, with the workflow status the comment
        carries for the board's selection."""

    @abstractmethod
    async def create_ticket(
        self, tracker: str, title: str, body: str, labels: Sequence[str]
    ) -> TicketCreated:
        """Open a ticket in ``tracker``; labels are the tracker's own, never
        new ones."""

    @abstractmethod
    async def update_ticket(
        self,
        url: str,
        *,
        body: str | None,
        title: str | None,
        add_labels: Sequence[str],
        remove_labels: Sequence[str],
        state: str | None,
        state_reason: str | None,
        parent: str | None = None,
        add_blocked_by: Sequence[str] = (),
        remove_blocked_by: Sequence[str] = (),
    ) -> TicketUpdated:
        """Change a ticket; a label is added or removed without touching the
        labels others set meanwhile, and removing one it does not carry
        changes nothing.

        Its relations to other tickets, named by their URLs, change the same
        way: ``parent`` is the ticket it becomes a part of (``""`` makes it
        part of none, ``None`` keeps it), and ``add_blocked_by`` /
        ``remove_blocked_by`` change the tickets it is blocked by. A ticket's
        children and the tickets it blocks change by updating those tickets."""

    @abstractmethod
    async def add_reaction(
        self, url: str, target_id: str, reaction: SemanticReaction
    ) -> ReactionAdded:
        """React to the comment of the ticket its reads named ``target_id``."""

    @abstractmethod
    async def candidates(self) -> list[Task]:
        """The member's actionable tickets in patrol order. A ticket of the
        ready lane blocked by an open ticket is not one: it is not started
        until its blockers are closed."""

    @abstractmethod
    async def refresh(self, task: Task) -> Task | None:
        """Re-read one candidate immediately before it is dispatched."""

    @abstractmethod
    async def move(self, task: Task, lane: MoveLane) -> bool:
        """Move the ticket to ``lane``.

        Returns:
            Whether it moved: a board without that lane leaves it where it is.
        """

    @abstractmethod
    async def add(self, ticket_url: str) -> bool:
        """Put the ticket on the board.

        Returns:
            Whether it is on it: ``False`` when no board is configured.
        """

    @abstractmethod
    async def closed_since(self, start: datetime, end: datetime) -> list[ClosedItem]:
        """The board's work closed between ``start`` and ``end``."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release resources owned by this service."""
