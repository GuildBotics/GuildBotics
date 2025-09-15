from logging import Logger

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
    ):
        """
        Initialize the WorkflowContext with a team, loader factory, and integration factory.
        Args:
            loader_factory (LoaderFactory): Factory for creating loaders.
            integration_factory (IntegrationFactory): Factory for creating integrations.
            brain_factory (BrainFactory): Factory for creating brains.
            logger (Logger): Logger instance for logging messages.
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

    @classmethod
    def get_default(
        cls,
        loader_factory: LoaderFactory,
        integration_factory: IntegrationFactory,
        brain_factory: BrainFactory,
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
