"""The one place a provider is chosen: each kind of service is the provider
``project.services`` names for it."""

from logging import Logger

from guildbotics.entities import Person, Service, Team
from guildbotics.integrations.chat_profile import (
    get_chat_slack_base_url,
)
from guildbotics.integrations.github.provider import GITHUB
from guildbotics.integrations.local.provider import LOCAL
from guildbotics.integrations.provider import Provider
from guildbotics.integrations.slack.slack_chat_service import SlackChatService
from guildbotics.runtime import IntegrationFactory
from guildbotics.runtime.chat_service import ChatService
from guildbotics.runtime.code_hosting_resources import RepositoryReadError
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.ticket_manager import TicketManager
from guildbotics.utils.i18n_tool import t

PROVIDERS: dict[str, Provider] = {
    provider.name: provider for provider in (GITHUB, LOCAL)
}


def configured_providers(team: Team) -> list[Provider]:
    """The providers the project's code host and board are, each once."""
    names = {
        team.project.get_service_name(service)
        for service in (Service.CODE_HOSTING_SERVICE, Service.TICKET_MANAGER)
    }
    return [provider for name, provider in PROVIDERS.items() if name in names]


class ServiceIntegrationFactory(IntegrationFactory):
    """The host's integration factory."""

    def create_code_hosting_service(
        self, logger: Logger, person: Person, team: Team
    ) -> CodeHostingService:
        provider = PROVIDERS.get(
            team.project.get_service_name(Service.CODE_HOSTING_SERVICE)
        )
        if provider is None or provider.code_hosting is None:
            raise RepositoryReadError(t("integrations.repository.unsupported"))
        return provider.code_hosting(logger, person, team)

    def create_ticket_manager(
        self, logger: Logger, person: Person, team: Team
    ) -> TicketManager:
        name = team.project.get_service_name(Service.TICKET_MANAGER)
        if not name:
            raise ValueError(
                "Issue tracking service name is required in the service configuration."
            )
        provider = PROVIDERS.get(name)
        if provider is None or provider.ticket_manager is None:
            raise ValueError(f"Unsupported issue tracking service: {name}")
        return provider.ticket_manager(logger, person, team)

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
