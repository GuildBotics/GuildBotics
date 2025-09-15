from abc import ABC, abstractmethod
from logging import Logger
from typing import Type

from pydantic import BaseModel


class Brain(ABC):

    def __init__(
        self,
        person_id: str,
        name: str,
        logger: Logger,
        description: str = "",
        instructions: str = "",
        response_class: Type[BaseModel] | None = None,
    ):
        """
        Initialize the Intelligence.
        Args:
            person_id (str): ID of the person using the intelligence.
            name (str): Name of the intelligence.
            logger (Logger): Logger instance for logging.
            description (str): Description of the intelligence.
            instructions (str): Instructions for using the intelligence.
            response_class (Type[BaseModel] | None): Class for the response model.
        """
        self.person_id = person_id
        self.name = name
        self.logger = logger
        self.description = description
        self.instructions = instructions
        self.response_class = response_class

    @abstractmethod
    async def run(self, message: str, **kwargs):
        pass
