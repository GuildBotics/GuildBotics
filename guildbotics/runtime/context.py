import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from guildbotics.entities.team import Person
from guildbotics.loader.yaml.yaml_team_loader import YamlTeamLoader
from guildbotics.runtime.brain import Brain
from guildbotics.runtime.brain_factory import BrainFactory
from guildbotics.runtime.chat_service import ChatService
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.integration_factory import IntegrationFactory
from guildbotics.runtime.ticket_manager import TicketManager
from guildbotics.utils.i18n_tool import set_language
from guildbotics.utils.import_utils import ClassResolver
from guildbotics.utils.log_utils import get_logger


class Context:
    """
    Context is a class that encapsulates the context for workflows.
    """

    def __init__(
        self,
        integration_factory: IntegrationFactory,
        brain_factory: BrainFactory,
        person: Person | None = None,
        message: str = "",
    ):
        """
        Initialize the context with the team the workspace's configuration
        holds, read anew for every context.
        Args:
            integration_factory (IntegrationFactory): Factory for creating integrations.
            brain_factory (BrainFactory): Factory for creating brains.
            person (Person | None): The current person in the context; a
                placeholder until :meth:`clone_for` names one.
            message (str): The message or prompt associated with the context.
        """
        self.integration_factory = integration_factory
        self.brain_factory = brain_factory
        self.logger = get_logger()
        self.team = YamlTeamLoader().load()
        set_language(self.team.project.get_language_code())
        self.person = person or Person(
            person_id="default_person", name="Default Person"
        )
        self.ticket_manager: TicketManager | None = None
        self.chat_service: ChatService | None = None
        self.code_hosting_service: CodeHostingService | None = None
        self.pipe = message
        self.shared_state: dict[str, Any] = {}
        self._invoker: Callable[[str, Any], Awaitable[Any]] | None = None

    @property
    def language_code(self) -> str:
        return self.team.project.get_language_code()

    @property
    def language_name(self) -> str:
        return self.team.project.get_language_name()

    def clone_for(self, person: Person) -> "Context":
        """
        Create a new context for a specific person.
        Args:
            person (Person): The person for whom the context is created.
        Returns:
            Context: A new context instance for the specified person.
        """
        return Context(self.integration_factory, self.brain_factory, person, self.pipe)

    def get_brain(
        self, name: str, config: dict | None, class_resolver: ClassResolver | None
    ) -> Brain:
        """
        Get a brain instance by name.
        Args:
            name (str): Name of the brain to get.
            config (dict | None): Optional configuration dictionary for the brain.
            class_resolver (ClassResolver | None): Optional class resolver for custom classes.
        Returns:
            Brain: An instance of the requested brain.
        """
        return self.brain_factory.create_brain(
            self.person.person_id,
            name,
            self.team.project.get_language_code(),
            self.logger,
            config,
            class_resolver,
        )

    def get_ticket_manager(self) -> TicketManager:
        """
        Get a ticket manager for the given person.
        Args:
            person (Person): The person for whom to get the ticket manager.
        Returns:
            TicketManager: An instance of the ticket manager for the person.
        """
        if self.ticket_manager is None:
            self.ticket_manager = self.integration_factory.create_ticket_manager(
                self.logger, self.person, self.team
            )
        return self.ticket_manager

    def get_code_hosting_service(self) -> CodeHostingService:
        """Get the configured code-hosting service for the current member."""
        if self.code_hosting_service is None:
            self.code_hosting_service = (
                self.integration_factory.create_code_hosting_service(
                    self.logger, self.person, self.team
                )
            )
        return self.code_hosting_service

    def get_chat_service(self) -> ChatService:
        """Get a chat service for the current person/team."""
        if self.chat_service is None:
            self.chat_service = self.integration_factory.create_chat_service(
                self.logger, self.person, self.team
            )
        return self.chat_service

    def set_invoker(self, invoker: Callable[[str, Any], Awaitable[Any]]) -> None:
        self._invoker = invoker

    async def invoke(self, name: str, /, *args: Any, **kwargs: Any) -> Any:
        if self._invoker is None:
            raise RuntimeError("Invoker function is not set.")
        return await self._invoker(name, *args, **kwargs)

    def update(self, key: str, value: Any, text_value: str) -> None:
        shared_value = self._normalize_for_shared_state(value)
        self.shared_state[key] = shared_value
        self.pipe = text_value

    async def aclose(self) -> None:
        """Close cached integrations that hold network resources."""
        await _maybe_aclose(self.code_hosting_service)
        self.code_hosting_service = None
        await _maybe_aclose(self.ticket_manager)
        self.ticket_manager = None
        await _maybe_aclose(self.chat_service)
        self.chat_service = None

    def _normalize_for_shared_state(self, value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump()
        if isinstance(value, list):
            if not value:
                return []
            if isinstance(value[0], BaseModel):
                return [item.model_dump() for item in value]
        if isinstance(value, dict):
            return deepcopy(value)
        return value


async def _maybe_aclose(obj: Any) -> None:
    if obj is None:
        return
    close = getattr(obj, "aclose", None)
    if not callable(close):
        return
    result = close()
    if asyncio.iscoroutine(result):
        await result
