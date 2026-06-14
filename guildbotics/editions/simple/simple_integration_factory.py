from logging import Logger

from guildbotics.entities import Person, Service, Team
from guildbotics.integrations.chat_profile import (
    get_chat_slack_base_url,
)
from guildbotics.integrations.chat_service import ChatService
from guildbotics.integrations.github.github_ticket_manager import GitHubTicketManager
from guildbotics.integrations.slack.slack_chat_service import SlackChatService
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

    def create_chat_service(
        self, logger: Logger, person: Person, team: Team
    ) -> ChatService:
        """
        Create a chat service for the given person.

        MVP: Slack only.
        """
        if not person.has_secret("SLACK_BOT_TOKEN"):
            env_key = person.to_person_env_key("SLACK_BOT_TOKEN")
            raise ValueError(
                f"Slack Bot Token is required for person '{person.person_id}'. "
                f"Set environment variable '{env_key}'."
            )
        token = person.get_secret("SLACK_BOT_TOKEN")
        return SlackChatService(
            logger=logger,
            token=token,
            base_url=get_chat_slack_base_url(person),
        )
