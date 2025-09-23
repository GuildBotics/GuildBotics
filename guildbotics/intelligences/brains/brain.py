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
        template_engine: str = "default",
        response_class: Type[BaseModel] | None = None,
    ):
        """
        Initialize the Intelligence.
        Args:
            person_id (str): ID of the person using the intelligence.
            name (str): Name of the intelligence.
            logger (Logger): Logger instance for logging.
            description (str): Description of the intelligence.
            template_engine (str): Template engine to use ("default" or "jinja2").
            response_class (Type[BaseModel] | None): Class for the response model.
        """
        self.person_id = person_id
        self.name = name
        self.logger = logger
        self.description = description
        self.template_engine = template_engine
        self.response_class = response_class

    @abstractmethod
    async def run(self, message: str, **kwargs):
        pass
