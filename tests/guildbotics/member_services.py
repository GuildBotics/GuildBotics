"""What the member capabilities read of a ``Context``, for their tests."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from guildbotics.entities.team import Person, Team
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.ticket_service import TicketService


@dataclass
class MemberServices:
    """The member, the team, and the services a capability is handed."""

    person: Person
    team: Team
    code: CodeHostingService
    tickets: TicketService | None = None
    logger: logging.Logger = logging.getLogger("tests.member_services")

    def get_code_hosting_service(self) -> CodeHostingService:
        return self.code

    def get_ticket_service(self) -> TicketService:
        assert self.tickets is not None, "no ticket service in this test"
        return self.tickets

    async def aclose(self) -> None:
        await self.code.aclose()
        if self.tickets is not None:
            await self.tickets.aclose()
