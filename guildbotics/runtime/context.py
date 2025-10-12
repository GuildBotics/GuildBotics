from copy import deepcopy
from logging import Logger
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from guildbotics.entities.task import Task
from guildbotics.entities.team import Person
from guildbotics.integrations.code_hosting_service import CodeHostingService
from guildbotics.integrations.ticket_manager import TicketManager
from guildbotics.intelligences.brains.brain import Brain
from guildbotics.runtime.brain_factory import BrainFactory
from guildbotics.runtime.integration_factory import IntegrationFactory
from guildbotics.runtime.loader_factory import LoaderFactory
from guildbotics.utils.i18n_tool import set_language
from guildbotics.utils.log_utils import get_logger


class Context:
    """
    Context is a class that encapsulates the context for workflows.
    """

    def __init__(
        self,
        loader_factory: LoaderFactory,
        integration_factory: IntegrationFactory,
        brain_factory: BrainFactory,
        logger: Logger,
        person: Person,
        task: Task,
        message: str,
    ):
        """
        Initialize the WorkflowContext with a team, loader factory, and integration factory.
        Args:
            loader_factory (LoaderFactory): Factory for creating loaders.
            integration_factory (IntegrationFactory): Factory for creating integrations.
            brain_factory (BrainFactory): Factory for creating brains.
            logger (Logger): Logger instance for logging messages.
            person (Person): The current person in the context.
            task (Task): The current task in the context.
            message (str): The message or prompt associated with the context.
        """
        self.loader_factory = loader_factory
        self.integration_factory = integration_factory
        self.brain_factory = brain_factory
        self.logger = logger
        self.team = loader_factory.create_team_loader().load()
        set_language(self.team.project.get_language_code())
        self.person = person
        self.task = task
        self.active_role = person.get_role(task.role)
        self.ticket_manager: TicketManager | None = None
        self.pipe = message
        self.shared_state: dict[str, Any] = {}
        self._invoker: Callable[[str, Any], Awaitable[Any]] | None = None

    @property
    def language_code(self) -> str:
        return self.team.project.get_language_code()

    @property
    def language_name(self) -> str:
        return self.team.project.get_language_name()

    @classmethod
    def get_default(
        cls,
        loader_factory: LoaderFactory,
        integration_factory: IntegrationFactory,
        brain_factory: BrainFactory,
        message: str,
    ) -> "Context":
        """
        Get the default context for the application.
        Args:
            loader_factory (LoaderFactory): Factory for creating loaders.
            integration_factory (IntegrationFactory): Factory for creating integrations.
            brain_factory (BrainFactory): Factory for creating brains.
        Returns:
            Context: An instance of the default context.
        """
        return cls(
            loader_factory,
            integration_factory,
            brain_factory,
            get_logger(),
            Person(person_id="default_person", name="Default Person"),
            Task(title="Default Task", description="This is a default task."),
            message,
        )

    def clone_for(self, person: Person) -> "Context":
        """
        Create a new context for a specific person.
        Args:
            person (Person): The person for whom the context is created.
        Returns:
            Context: A new context instance for the specified person.
        """
        return Context(
            self.loader_factory,
            self.integration_factory,
            self.brain_factory,
            get_logger(),
            person,
            self.task,
            self.pipe,
        )

    def update_task(self, task: Task) -> None:
        """
        Update the current task in the context.
        Args:
            task (Task): The new task to set in the context.
        """
        self.task = task
        self.active_role = self.person.get_role(task.role)

    def get_brain(self, name: str) -> Brain:
        """
        Get a brain instance by name.
        Args:
            name (str): Name of the brain to get.
        Returns:
            Brain: An instance of the requested brain.
        """
        return self.brain_factory.create_brain(
            self.person.person_id,
            name,
            self.team.project.get_language_code(),
            self.logger,
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

    def get_code_hosting_service(
        self, repository: str | None = None
    ) -> CodeHostingService:
        """
        Get a code hosting service for the given person and optional repository.
        Args:
            person (Person): The person for whom to get the code hosting service.
            repository (str | None): The git repository associated with the code hosting service.
        Returns:
            CodeHostingService: An instance of the code hosting service for the person and team.
        """
        return self.integration_factory.create_code_hosting_service(
            self.person, self.team, repository
        )

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
