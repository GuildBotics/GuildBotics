from abc import ABC, abstractmethod
from datetime import datetime
from logging import Logger

from guildbotics.entities import Person, Task, Team
from guildbotics.runtime.code_hosting_service import ClosedItem


class TicketManager(ABC):
    """The board a member takes tickets from.

    The tickets themselves (issues, their comments) are the code host's
    (:class:`~guildbotics.runtime.code_hosting_service.CodeHostingService`);
    the board knows which of them are in which lane and whose they are.
    """

    def __init__(self, logger: Logger, person: Person, team: Team):
        """
        Args:
            logger (Logger): Logger instance for logging messages.
            person (Person): The person associated with the ticket manager.
            team (Team): The team associated with the ticket manager.
        """
        self.logger = logger
        self.person = person
        self.team = team

    @abstractmethod
    async def get_task_candidates(self) -> list[Task]:
        """Return the member's actionable tickets in patrol order."""

    @abstractmethod
    async def refresh_task(self, task: Task) -> Task | None:
        """Re-read one candidate immediately before it is dispatched."""

    @abstractmethod
    async def move_ticket(self, task: Task, new_status: str) -> bool:
        """
        Move a ticket to a new status.

        Args:
            task (Task): The task representing the ticket to move.
            new_status (str): The new status to assign to the ticket.

        Returns:
            bool: True if the ticket was actually moved, False if the move was a
                no-op (e.g. the target lane is not configured or cannot be
                resolved). Callers should not assume the new status took effect
                when this returns False.
        """

    @abstractmethod
    async def get_ticket_url(self, task: Task, markdown: bool = True) -> str:
        """
        Get the URL for the task in the ticket management system.

        Args:
            task (Task): The task representing the ticket.
            markdown (bool): Whether to format the URL for Markdown.

        Returns:
            str: The URL for the task.
        """

    @abstractmethod
    async def add_ticket(self, issue_url: str) -> str | None:
        """Put the issue on the board; the board's id of it, or ``None`` when
        no board is configured to put it on."""

    @abstractmethod
    async def closed_since(self, start: datetime, end: datetime) -> list[ClosedItem]:
        """The board's work closed between ``start`` and ``end``."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release resources owned by this manager."""
