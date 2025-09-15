from abc import ABC, abstractmethod
from logging import Logger

from guildbotics.entities import Person, Team
from guildbotics.integrations.code_hosting_service import CodeHostingService
from guildbotics.integrations.ticket_manager import TicketManager


class IntegrationFactory(ABC):
    @abstractmethod
    def create_ticket_manager(
        self, logger: Logger, person: Person, team: Team
    ) -> TicketManager:
        """
        Create a ticket manager for the given person.
        Args:
            logger (Logger): Logger instance for logging messages.
            person (Person): The person associated with the ticket manager.
            team (Team): The team associated with the ticket manager.
        Returns:
            TicketManager: An instance of a ticket manager for the person.
        """
        pass

    @abstractmethod
    def create_code_hosting_service(
        self, person: Person, team: Team, repository: str | None = None
    ) -> CodeHostingService:
        """
        Create a code hosting service manager for the given person and team.
        Args:
            person (Person): The person associated with the code hosting service.
            team (Team): The team associated with the code hosting service.
            repository (str | None): The git repository associated with the code hosting service.
        Returns:
            CodeHostingService: An instance of a code hosting service manager for the person and team.
        """
        pass
