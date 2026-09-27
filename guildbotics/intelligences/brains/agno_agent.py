import json
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from guildbotics.intelligences.brains.brain import (
    Brain,
    ExecutionMetadata,
    public_parameters,
)
from guildbotics.intelligences.brains.inference import AgnoCall, inference
from guildbotics.intelligences.brains.util import to_plain_text, to_response_class
from guildbotics.intelligences.effort import resolve_effort, validate_effort_overlay
from guildbotics.observability import span_scope
from guildbotics.utils.fileio import (
    get_person_config_path,
    get_template_path,
    load_person_slot_mapping,
    load_yaml_file,
)
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

#: Overrides keyed by person id. Production code does not write this dict.
#: See ``simple_brain_factory.person_brain_mapping`` for why.
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
    """Return the person's model slots, read from configuration each call.

    An entry in :data:`person_model_mapping` is an override and wins over the
    files. Otherwise the mapping is loaded and not stored, so the next brain
    sees a file that changed after the previous brain was built.

    Args:
        person_id (str): The person whose ``model_mapping.yml`` to read.

    Returns:
        dict[str, ModelConfig]: Slot name to the validated model definition.
    """
    override = person_model_mapping.get(person_id)
    if override is not None:
        return override

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

    return model_mapping


class AgnoAgentDefaultBrain(Brain):
    def __init__(self, *args: Any, model: str = "default", **kwargs: Any):
        """Take the :class:`Brain` arguments, and the model slot to run."""
        super().__init__(*args, **kwargs)
        self.model_config = get_model_mapping(self.person_id)[model]
        self.model_slot = model

    @property
    def configuration(self) -> dict[str, Any]:
        return {
            **super().configuration,
            "slot": self.model_slot,
            "definition": self.model_config.name,
            "provider": self.model_config.model_class,
            "restricted_model": self.model_config.is_restricted_model,
            "model": str(self.model_config.parameters.get("id", "")),
            "parameters": public_parameters(self.model_config.parameters),
        }

    async def run(
        self,
        message: str,
        *,
        session_state: dict[str, Any] | None = None,
        response_model: type[BaseModel] | None = None,
        **_: Any,
    ):
        """Expand the template, have the slot's model answer it on the host
        (:func:`inference`), and read the answer as the response class."""
        state = session_state or {}
        description = replace_placeholders(
            self.description, state, self.template_engine
        )
        response_class = response_model or self.response_class
        output_schema = response_class.model_json_schema() if response_class else None
        if self.model_config.is_restricted_model:
            message = to_plain_text(description, message, response_class)
            output_schema = None
        with span_scope("llm") as span:
            answer = await inference().agno(
                self.person_id,
                AgnoCall(
                    brain=self.name,
                    slot=self.model_slot,
                    effort=resolve_effort(state, self.effort, logger=self.logger),
                    description=description,
                    message=self.patch_message(message),
                    output_schema=output_schema,
                    session_state=_agent_session_state(state),
                    span=span,
                ),
            )
        content = answer.content
        if response_class:
            content = to_response_class(
                content if isinstance(content, str) else json.dumps(content),
                response_class,
            )
        self.execution = ExecutionMetadata(model=answer.model, usage=answer.usage)
        return content

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


#: Session state entries the agent must not receive. ``context`` is the live
#: runtime handle: it owns open HTTP clients, so agno's per-run
#: ``deepcopy(session_state)`` raises on it, and its repr means nothing to a
#: model. Everything else is prompt data agno resolves placeholders from.
_RUNTIME_ONLY_SESSION_KEYS = frozenset({"context"})


def _agent_session_state(session_state: object) -> dict[str, Any]:
    """The prompt-facing part of the session state, safe to hand to agno."""
    if not isinstance(session_state, dict):
        return {}
    return {
        key: value
        for key, value in session_state.items()
        if key not in _RUNTIME_ONLY_SESSION_KEYS
    }
