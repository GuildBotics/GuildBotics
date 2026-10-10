"""The one place a provider is chosen: each kind of service is the provider
``project.services`` names for it."""

from collections.abc import Callable
from logging import Logger

from guildbotics.entities import Person, Service, Team
from guildbotics.integrations.event_listener import EventListener
from guildbotics.integrations.github.provider import GITHUB
from guildbotics.integrations.local.provider import LOCAL
from guildbotics.integrations.provider import Chat, Provider
from guildbotics.integrations.slack.provider import SLACK
from guildbotics.runtime import IntegrationFactory
from guildbotics.runtime.chat_service import ChatService, ChatServiceError
from guildbotics.runtime.code_hosting_resources import RepositoryReadError
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.ticket_manager import TicketManager
from guildbotics.utils.i18n_tool import t

PROVIDERS: dict[str, Provider] = {
    provider.name: provider for provider in (GITHUB, LOCAL, SLACK)
}


def configured_providers(team: Team) -> list[Provider]:
    """The providers the project's services are, each once."""
    names = {team.project.get_service_name(service) for service in Service}
    return [provider for name, provider in PROVIDERS.items() if name in names]


def chat_provider_name(team: Team) -> str:
    """The name of the provider the project's chat is, which keys what is
    recorded of its chat.

    Raises:
        ChatServiceError: If the project names none, or one that has no chat.
    """
    _chat(team)
    return team.project.get_service_name(Service.CHAT_SERVICE)


def _chat(team: Team) -> Chat:
    name = team.project.get_service_name(Service.CHAT_SERVICE)
    provider = PROVIDERS.get(name)
    if provider is None or provider.chat is None:
        raise ChatServiceError(t("integrations.chat.unsupported", name=name))
    return provider.chat


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
        return _chat(team).service(logger, person, team)

    def create_event_listener(
        self,
        logger: Logger,
        team: Team,
        persons: list[Person],
        on_activity: Callable[[], None],
    ) -> EventListener:
        """The listener of the connection ``persons`` share, by their
        :meth:`listener_key`."""
        return _chat(team).event_listener(logger, team, persons, on_activity)

    def listener_key(self, person: Person, team: Team) -> str:
        """The connection ``person``'s chat events arrive on.

        Raises:
            ChatServiceError: If the person cannot connect.
        """
        return _chat(team).listener_key(person, team)
