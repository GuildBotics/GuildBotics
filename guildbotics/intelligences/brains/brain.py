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


def public_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Snapshot known inference settings, never arbitrary provider credentials."""
    keys = {
        "id",
        "model",
        "temperature",
        "top_p",
        "top_k",
        "seed",
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "reasoning_effort",
        "effort",
        "thinking_budget",
    }
    result: dict[str, Any] = {
        key: value
        for key, value in parameters.items()
        if key in keys and (value is None or isinstance(value, (str, int, float, bool)))
    }
    thinking = parameters.get("thinking")
    if isinstance(thinking, dict):
        result["thinking"] = {
            key: value
            for key, value in thinking.items()
            if key in {"type", "budget_tokens"}
            and (value is None or isinstance(value, (str, int, float, bool)))
        }
    return result


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

    @property
    def configuration(self) -> dict[str, Any]:
        """Public resolved configuration available before an invocation."""
        return {"class": f"{type(self).__module__}.{type(self).__qualname__}"}

    @abstractmethod
    async def run(self, message: str, **kwargs):
        pass
