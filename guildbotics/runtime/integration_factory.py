from abc import ABC, abstractmethod
from logging import Logger

from guildbotics.entities import Person, Team
from guildbotics.runtime.chat_service import ChatService
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.ticket_service import TicketService


class IntegrationFactory(ABC):
    @abstractmethod
    def create_code_hosting_service(
        self, logger: Logger, person: Person, team: Team
    ) -> CodeHostingService:
        """The member's code host."""

    @abstractmethod
    def create_ticket_service(
        self, logger: Logger, person: Person, team: Team
    ) -> TicketService:
        """The member's tickets and board."""

    @abstractmethod
    def create_chat_service(
        self, logger: Logger, person: Person, team: Team
    ) -> ChatService:
        """The member's chat."""
