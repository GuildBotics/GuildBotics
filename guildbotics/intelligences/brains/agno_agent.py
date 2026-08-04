import time
from copy import deepcopy
from logging import Logger
from pathlib import Path
from typing import Any, cast

from agno.agent import Agent
from agno.models.base import Model
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from guildbotics.intelligences.brains.brain import Brain
from guildbotics.intelligences.brains.util import (
    summary_log_line,
    to_plain_text,
    to_response_class,
)
from guildbotics.intelligences.effort import (
    effort_diagnostics,
    effort_settings,
    resolve_effort,
    validate_effort_overlay,
)
from guildbotics.observability import span_scope
from guildbotics.observability.diagnostics_events import (
    record_correlated_io,
    record_span_summary,
)
from guildbotics.utils.fileio import (
    get_person_config_path,
    get_template_path,
    load_person_slot_mapping,
    load_yaml_file,
)
from guildbotics.utils.import_utils import instantiate_class
from guildbotics.utils.rate_limiter import acquire
from guildbotics.utils.text_utils import replace_placeholders


class RateLimit(BaseModel):
    """Rate limiting configuration for the model.

    Attributes:
        max_requests_per_minute (Optional[int]): Max requests allowed per minute.
        max_requests_per_day (Optional[int]): Max requests allowed per day.
    """

    max_requests_per_minute: int | None = None
    max_requests_per_day: int | None = None


class ModelConfig(BaseModel):
    """Model configuration."""

    name: str
    model_class: str
    parameters: dict = {}
    rate_limit: RateLimit | None = None
    is_restricted_model: bool = False
    #: Effort level -> provider parameters shallow-merged into ``parameters``.
    #: Replacing ``id`` here is allowed, so a level may switch models entirely.
    effort: dict[str, dict] = Field(default_factory=dict)

    @field_validator("effort", mode="before")
    @classmethod
    def _validate_effort(cls, value: Any, info: ValidationInfo) -> dict[str, dict]:
        return validate_effort_overlay(
            value, where=f"model '{info.data.get('name', '')}'"
        )


#: `models/<provider>/<slot>.yml`
MODEL_PATH_PARTS = 3

person_model_mapping: dict[str, dict[str, ModelConfig]] = {}


def _inherited_effort(person_id: str, model_file: str) -> dict:
    """The effort mapping a model definition inherits when it states none.

    Two fallbacks, in order:

    1. the provider's ``default.yml`` -- slots live at
       ``models/<provider>/<slot>.yml`` and only ``default.yml`` is packaged, so
       a second slot on the same provider would otherwise have no mapping;
    2. the packaged definition of the same path -- a workspace file shadows the
       template wholesale, so a definition written before it had an ``effort:``
       block would otherwise permanently lose the provider's mapping.

    Stating ``effort: {}`` still means "none": only an absent key inherits.
    """
    from guildbotics.intelligences.llm_providers import PROVIDER_DEFAULT_FILENAME

    parts = model_file.split("/")
    if len(parts) < MODEL_PATH_PARTS:
        return {}
    provider_default = f"{'/'.join(parts[:-1])}/{PROVIDER_DEFAULT_FILENAME}"
    if parts[-1] != PROVIDER_DEFAULT_FILENAME:
        effort = _effort_of(
            get_person_config_path(person_id, f"intelligences/{provider_default}")
        )
        if effort:
            return effort
    # The packaged fallback is the provider's own default definition: only
    # `default.yml` is packaged, so this slot's own path would find nothing.
    return _effort_of(get_template_path() / f"intelligences/{provider_default}")


def _effort_of(path: Path) -> dict:
    if not path.exists():
        return {}
    data = load_yaml_file(path)
    return data.get("effort", {}) if isinstance(data, dict) else {}


def get_model_mapping(person_id: str) -> dict[str, ModelConfig]:
    if person_id in person_model_mapping:
        return person_model_mapping[person_id]

    mapping = load_person_slot_mapping(person_id, "intelligences/model_mapping.yml")
    model_mapping = {}
    for name, model_file in mapping.items():
        model_file_path = get_person_config_path(
            person_id, f"intelligences/{model_file}"
        )
        model = cast(dict, load_yaml_file(model_file_path))
        model["name"] = model_file
        if model.get("effort") is None:
            model["effort"] = _inherited_effort(person_id, str(model_file))
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
        response_class: type[BaseModel] | None = None,
        model: str = "default",
        effort: str = "",
    ):
        super().__init__(
            person_id=person_id,
            name=name,
            logger=logger,
            description=description,
            template_engine=template_engine,
            response_class=response_class,
            effort=effort,
        )
        model_mapping = get_model_mapping(person_id)
        self.model_config = model_mapping[model]

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
            if "response_model" not in kwargs and self.response_class:
                kwargs["response_model"] = self.response_class

        kwargs["tool_call_limit"] = kwargs.get("tool_call_limit", 5)

        resolved = resolve_effort(
            kwargs.get("session_state"), self.effort, logger=self.logger
        )
        overlay = effort_settings(
            self.model_config.effort, resolved, logger=self.logger
        )
        model_parameters = deepcopy(self.model_config.parameters)
        model_parameters.update(deepcopy(overlay))
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
        message = self.patch_message(message)
        # The request is made with this id, so it is the effective model whether
        # or not the call succeeds. The effort level is effective only when it
        # actually contributed settings.
        model_id = str(model_parameters.get("id", "") or "")
        applied_effort = resolved.resolved if overlay else ""
        with span_scope("llm"):
            started = time.monotonic()
            self._write_request_io(
                message,
                description,
                kwargs,
                effort_diagnostics(resolved, overlay, model=model_id),
            )
            try:
                response = await agent.arun(message)
            except Exception:
                self._record_summary("failed", started, model_id, applied_effort)
                raise
            content = response.content
            self._write_response_io(content)
            self._record_summary(
                "finished",
                started,
                model_id,
                applied_effort,
                usage=_response_usage(response),
            )
        if self.response_class and (
            self.model_config.is_restricted_model
            or not isinstance(content, self.response_class)
        ):
            content = to_response_class(str(content), self.response_class)

        return content

    def _record_summary(
        self,
        status: str,
        started: float,
        model: str,
        effort: str,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """Close the span with the model the request was really made with.

        A definition that names no model id of its own leaves the span's model
        empty — the request ran on the provider's default, which is unknown —
        while ``model.slot`` still names the slot so traces stay searchable.
        """
        duration_ms = (time.monotonic() - started) * 1000
        record_span_summary(
            status=status,
            model=model,
            effort=effort,
            duration_ms=duration_ms,
            usage=usage,
            attributes={"model.slot": self.model_config.name},
        )
        self.logger.info(
            summary_log_line(
                "llm",
                self.model_config.name,
                status,
                duration_ms=duration_ms,
                model=model,
                effort=effort,
            )
        )

    def patch_message(self, message: str) -> str:
        """
        Ensures the message passed to the agent is not empty.

        Args:
            message (str): The input message to be processed by the agent.

        Returns:
            str: The original message if provided; otherwise, a default instruction
                 ("Execute exactly as specified in the system message.") to ensure
                 the agent always receives a valid command.

        This patching is necessary to prevent errors or undefined behavior when the agent
        receives an empty message, by supplying a clear default instruction.
        """
        if message:
            return message

        return "Execute exactly as specified in the system message."

    def _write_request_io(
        self,
        message: str,
        description: str,
        kwargs: dict,
        effort: dict[str, Any],
    ) -> None:
        record_correlated_io(
            io_type="llm.request",
            payload={
                "effort": effort,
                "person_id": self.person_id,
                "brain": self.name,
                "model": self.model_config.name,
                "model_class": self.model_config.model_class,
                "restricted_model": self.model_config.is_restricted_model,
                "response_class": (
                    self.response_class.__name__ if self.response_class else ""
                ),
                "description": description,
                "message": message,
                "session_state": kwargs.get("session_state", {}),
                "add_state_in_messages": kwargs.get("add_state_in_messages", False),
            },
        )

    def _write_response_io(self, content: object) -> None:
        record_correlated_io(
            io_type="llm.response",
            payload={
                "person_id": self.person_id,
                "brain": self.name,
                "model": self.model_config.name,
                "content": content,
            },
        )


def _response_usage(response: object) -> dict[str, Any]:
    metrics = getattr(response, "metrics", None)
    if isinstance(metrics, BaseModel):
        return cast(dict[str, Any], metrics.model_dump())
    if isinstance(metrics, dict):
        return {str(key): value for key, value in metrics.items()}
    return {}
