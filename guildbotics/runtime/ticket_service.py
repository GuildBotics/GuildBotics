"""Read resources through the member command window."""

from abc import ABC, abstractmethod
from typing import Any

from guildbotics.runtime.resources import ReadPage


class TicketService(ABC):
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
        ``subject`` being the ticket's URL, or the repository path for
        ``tracker_tickets`` (see
        :meth:`~.code_hosting_service.CodeHostingService.read`)."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release resources owned by this service."""
