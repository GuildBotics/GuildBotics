from copy import deepcopy
from logging import Logger
from typing import Optional, Type, cast

from agno.agent import Agent
from agno.models.base import Model
from agno.utils import log
from pydantic import BaseModel

from guildbotics.intelligences.brains.brain import Brain
from guildbotics.intelligences.brains.util import to_plain_text, to_response_class
from guildbotics.utils.fileio import get_person_config_path, load_yaml_file
from guildbotics.utils.import_utils import instantiate_class
from guildbotics.utils.log_utils import get_file_handler
from guildbotics.utils.rate_limiter import acquire
from guildbotics.utils.text_utils import replace_placeholders


class RateLimit(BaseModel):
    """Rate limiting configuration for the model.

    Attributes:
        max_requests_per_minute (Optional[int]): Max requests allowed per minute.
        max_requests_per_day (Optional[int]): Max requests allowed per day.
    """

    max_requests_per_minute: Optional[int] = None
    max_requests_per_day: Optional[int] = None


class ModelConfig(BaseModel):
    """Model configuration."""

    name: str
    model_class: str
    parameters: dict = {}
    rate_limit: Optional[RateLimit] = None
    is_restricted_model: bool = False


person_model_mapping: dict[str, dict[str, ModelConfig]] = {}


def get_model_mapping(person_id: str) -> dict[str, ModelConfig]:
    if person_id in person_model_mapping:
        return person_model_mapping[person_id]

    config_file = get_person_config_path(person_id, "intelligences/model_mapping.yml")
    mapping = cast(dict, load_yaml_file(config_file))
    model_mapping = {}
    for name, model_file in mapping.items():
        model_file_path = get_person_config_path(
            person_id, f"intelligences/{model_file}"
        )
        model = cast(dict, load_yaml_file(model_file_path))
        model["name"] = model_file
        model_mapping[name] = ModelConfig.model_validate(model)

    person_model_mapping[person_id] = model_mapping
    return model_mapping


class AgnoAgentDefaultBrain(Brain):

    def __init__(
        self,
        person_id: str,
        name: str,
        logger: Logger,
        description: str = "",
        template_engine: str = "default",
        response_class: Type[BaseModel] | None = None,
        model: str = "default",
    ):
        super().__init__(
            person_id=person_id,
            name=name,
            logger=logger,
            description=description,
            template_engine=template_engine,
            response_class=response_class,
        )
        model_mapping = get_model_mapping(person_id)
        self.model_config = model_mapping[model]
        file_handler = get_file_handler()
        if file_handler and file_handler not in log.logger.handlers:
            log.logger.addHandler(file_handler)

    async def run(self, message: str, **kwargs):
        kwargs["name"] = kwargs.get("name", self.name)

        description = kwargs.pop("description", self.description)
        description = replace_placeholders(
            description, kwargs.get("session_state", {}), self.template_engine
        )
        if self.model_config.is_restricted_model:
            response_class = kwargs.pop("response_model", self.response_class)
            message = to_plain_text(description, message, response_class)
        else:
            kwargs["description"] = description
            if not "response_model" in kwargs and self.response_class:
                kwargs["response_model"] = self.response_class

        kwargs["tool_call_limit"] = kwargs.get("tool_call_limit", 5)

        model_parameters = deepcopy(self.model_config.parameters)
        model = instantiate_class(
            self.model_config.model_class, expected_type=Model, **model_parameters
        )
        kwargs["model"] = model
        if "cwd" in kwargs:
            kwargs.pop("cwd")

        agent = Agent(**kwargs)

        if (
            self.model_config.rate_limit
            and self.model_config.rate_limit.max_requests_per_minute
        ):
            await acquire(
                self.model_config.name,
                self.model_config.rate_limit.max_requests_per_minute,
            )
        response = await agent.arun(message)
        content = response.content
        if self.response_class and (
            self.model_config.is_restricted_model
            or not isinstance(content, self.response_class)
        ):
            content = to_response_class(str(content), self.response_class)

        return content
