from abc import ABC, abstractmethod
from logging import Logger

from guildbotics.intelligences.brains.brain import Brain


class BrainFactory(ABC):
    """
    Factory class for creating brain instances.
    """

    @abstractmethod
    def create_brain(
        self, person_id: str, name: str, language_code: str, logger: Logger
    ) -> Brain:
        """
        Create an brain instance by name.

        Args:
            person_id (str): ID of the person for whom the brain is created.
            name (str): Name of the brain to create.
            language_code (str): Language code for the brain.
            logger (Logger): Logger instance for logging.

        Returns:
            brain: An instance of the requested brain.
        """
        pass
