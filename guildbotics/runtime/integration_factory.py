from abc import ABC, abstractmethod
from logging import Logger

from guildbotics.entities import Person, Team
from guildbotics.integrations.chat_service import ChatService
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
    def create_chat_service(
        self, logger: Logger, person: Person, team: Team
    ) -> ChatService:
        """
        Create a chat service for the given person and team.

        Args:
            logger (Logger): Logger instance for logging messages.
            person (Person): The person associated with the chat service.
            team (Team): The team associated with the chat service.

        Returns:
            ChatService: An instance of a chat service for the person.
        """
        pass
