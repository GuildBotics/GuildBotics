from logging import Logger

from guildbotics.entities import Person, Service, Team
from guildbotics.integrations.code_hosting_service import CodeHostingService
from guildbotics.integrations.github.github_code_hosting_service import (
    GitHubCodeHostingService,
)
from guildbotics.integrations.github.github_ticket_manager import GitHubTicketManager
from guildbotics.integrations.ticket_manager import TicketManager
from guildbotics.runtime import IntegrationFactory


class SimpleIntegrationFactory(IntegrationFactory):
    """
    Default integration factory for creating message pollers.
    """

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
        name = team.project.get_service_name(Service.TICKET_MANAGER)
        if not name:
            raise ValueError(
                "Issue tracking service name is required in the service configuration."
            )
        if name == "github":
            return GitHubTicketManager(logger, person, team)
        raise ValueError(f"Unsupported issue tracking service: {name}")

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
        name = team.project.get_service_name(Service.CODE_HOSTING_SERVICE)
        if name == "github":
            return GitHubCodeHostingService(person, team, repository)
        raise ValueError(f"Unsupported code hosting service: {name}")
