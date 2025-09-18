from abc import ABC, abstractmethod
from logging import Logger

from guildbotics.entities import Person, Task, Team


class TicketManager(ABC):

    def __init__(self, logger: Logger, person: Person, team: Team):
        """
        Initialize the ticket manager.
        This class is designed to be subclassed for different ticket management systems.
        It provides an interface for creating, closing, and retrieving tickets.
        Subclasses should implement the abstract methods to define
        how to manage tickets in the specific system.

        Args:
            logger (Logger): Logger instance for logging messages.
            person (Person): The person associated with the ticket manager.
            team (Team): The team associated with the ticket manager.
        """
        self.logger = logger
        self.person = person
        self.team = team

    @abstractmethod
    async def create_tickets(self, tasks: list[Task]):
        """Create tickets based on the provided tasks."""
        pass

    @abstractmethod
    async def get_task_to_work_on(self) -> Task | None:
        """
        Retrieve a ticket that the person can work on.
        This method should return a Task object representing the ticket
        that the person can work on. If no tickets are available, it should return None.
        Returns:
            Task: A task representing the ticket to work on, or None if no tickets are available.
        """
        pass

    @abstractmethod
    async def move_ticket(self, task: Task, new_status: str) -> None:
        """
        Move a ticket to a new status.

        Args:
            task (Task): The task representing the ticket to move.
            new_status (str): The new status to assign to the ticket.
        """
        pass

    @abstractmethod
    async def add_comment_to_ticket(self, task: Task, comment: str) -> None:
        """
        Add a comment to an existing ticket.

        Args:
            task (Task): The task representing the ticket to which the comment will be added.
            comment (str): The comment to add to the ticket.
        """
        pass

    @abstractmethod
    async def get_ticket_url(self, task: Task, markdown: bool = True) -> str:
        """
        Get the URL for the task in the ticket management system.

        Args:
            task (Task): The task representing the ticket.
            markdown (bool): Whether to format the URL for Markdown.

        Returns:
            str: The URL for the task.
        """
        pass

    @abstractmethod
    def get_board_url(self) -> str:
        """
        Get the URL for the board in the ticket management system.

        Returns:
            str: The URL for the board.
        """
        pass

    @abstractmethod
    async def update_ticket(self, task: Task) -> None:
        """
        Update an existing ticket with the latest information from the task.

        Args:
            task (Task): The task representing the ticket to update.
        """
        pass
