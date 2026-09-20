from abc import ABC, abstractmethod
from dataclasses import dataclass
from logging import Logger
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class ExecutionMetadata:
    """Provider-reported facts about the last completed invocation."""

    model: str = ""
    usage: dict[str, Any] | None = None
    cost: float | None = None
    retries: int | None = None


class Brain(ABC):
    probabilistic_answers = False

    def __init__(
        self,
        person_id: str,
        name: str,
        logger: Logger,
        description: str = "",
        template_engine: str = "default",
        response_class: type[BaseModel] | None = None,
        effort: str = "",
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
            effort (str): Frontmatter effort level; a runtime request overrides it.
        """
        self.person_id = person_id
        self.name = name
        self.logger = logger
        self.description = description
        self.template_engine = template_engine
        self.response_class = response_class
        self.effort = effort
        self.execution = ExecutionMetadata()

    @abstractmethod
    async def run(self, message: str, **kwargs):
        pass
