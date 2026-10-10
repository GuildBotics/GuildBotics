"""What the member capabilities read of a ``Context``, for their tests."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from guildbotics.entities.team import Person, Team
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.ticket_manager import TicketManager


@dataclass
class MemberServices:
    """The member, the team, and the services a capability is handed."""

    person: Person
    team: Team
    code: CodeHostingService
    board: TicketManager | None = None
    logger: logging.Logger = logging.getLogger("tests.member_services")

    def get_code_hosting_service(self) -> CodeHostingService:
        return self.code

    def get_ticket_manager(self) -> TicketManager:
        assert self.board is not None, "no board in this test"
        return self.board

    async def aclose(self) -> None:
        await self.code.aclose()
        if self.board is not None:
            await self.board.aclose()
