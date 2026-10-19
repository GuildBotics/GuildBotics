"""The one place a provider is chosen: each kind of service is the provider
``project.services`` names for it."""

from collections.abc import Callable, Coroutine, Mapping
from logging import Logger
from pathlib import Path
from typing import Any

from guildbotics.entities import Person, Service, Team
from guildbotics.integrations.branvil.provider import BRANVIL
from guildbotics.integrations.event_listener import EventListener
from guildbotics.integrations.github.provider import GITHUB
from guildbotics.integrations.local.provider import LOCAL
from guildbotics.integrations.provider import Chat, Provider
from guildbotics.integrations.slack.provider import SLACK
from guildbotics.runtime import IntegrationFactory
from guildbotics.runtime.chat_service import ChatService
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.port import ServiceError
from guildbotics.runtime.ticket_service import TicketService
from guildbotics.utils.i18n_tool import t

PROVIDERS: dict[str, Provider] = {
    provider.name: provider for provider in (BRANVIL, GITHUB, LOCAL, SLACK)
}


def hub_ports(services: Mapping[str, Mapping[str, Any]]) -> set[int]:
    """The ports of the hub machine the project's services are reached at,
    which every other machine forwards to the hub."""
    return {
        port
        for provider in PROVIDERS.values()
        if (port := provider.hub_port(services)) is not None
    }


def configured_providers(team: Team) -> list[Provider]:
    """The providers the project's services are, each once."""
    names = {team.project.get_service_name(service) for service in Service}
    return [provider for name, provider in PROVIDERS.items() if name in names]


def ci_image_loader(
    team: Team,
) -> Callable[[Path], Coroutine[Any, Any, list[str]]] | None:
    """How the project's code host loads an image archive into its CI runner
    (see :attr:`Provider.load_ci_image`); ``None`` when GuildBotics loads no
    image into its CI."""
    name = team.project.get_service_name(Service.CODE_HOSTING_SERVICE)
    provider = PROVIDERS.get(name)
    return provider.load_ci_image if provider else None


def chat_provider_name(team: Team) -> str:
    """The name of the provider the project's chat is, which keys what is
    recorded of its chat.

    Raises:
        ServiceError: If the project names none, or one that has no chat.
    """
    _chat(team)
    return team.project.get_service_name(Service.CHAT_SERVICE)


def _served(team: Team, service: Service) -> Any:
    """What the provider the project names for ``service`` makes of it.

    Raises:
        ServiceError: If the project names none, or one that does not serve it.
    """
    name = team.project.get_service_name(service)
    made = getattr(PROVIDERS.get(name), service.value, None)
    if made is None:
        raise ServiceError(
            t("integrations.resources.unsupported", service=service.value, name=name)
        )
    return made


def _chat(team: Team) -> Chat:
    return _served(team, Service.CHAT_SERVICE)


class ServiceIntegrationFactory(IntegrationFactory):
    """The host's integration factory."""

    def create_code_hosting_service(
        self, logger: Logger, person: Person, team: Team
    ) -> CodeHostingService:
        return _served(team, Service.CODE_HOSTING_SERVICE)(logger, person, team)

    def create_ticket_service(
        self, logger: Logger, person: Person, team: Team
    ) -> TicketService:
        return _served(team, Service.TICKET_SERVICE)(logger, person, team)

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
            ServiceError: If the person cannot connect.
        """
        return _chat(team).listener_key(person, team)
