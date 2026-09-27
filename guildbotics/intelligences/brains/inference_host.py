"""The external inference calls, made on the host where their keys are.

A brain asks for them through :func:`~guildbotics.intelligences.brains.
inference.inference`: on the host, here directly; inside a command's isolated
environment, through the command's window, whose grant answers it here. Either
way what is called is settled here from the host's own settings -- the model
of the member's slot, its effort mapping, its rate limit -- and the call is
recorded here, under the span the brain opened.
"""

from __future__ import annotations

import re
import time
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
from agno.agent import Agent
from agno.models.base import Model
from pydantic import BaseModel, ConfigDict

from guildbotics.intelligences.brains.agno_agent import get_model_mapping
from guildbotics.intelligences.brains.inference import AgnoAnswer, AgnoCall, JevCall
from guildbotics.intelligences.brains.jev import credential
from guildbotics.intelligences.brains.span_summary import record_summary
from guildbotics.intelligences.effort import effort_diagnostics, effort_settings
from guildbotics.observability import bind_span
from guildbotics.observability.diagnostics_events import record_correlated_io
from guildbotics.utils.fileio import get_workspace_config_dir
from guildbotics.utils.import_utils import instantiate_class
from guildbotics.utils.log_utils import get_logger
from guildbotics.utils.rate_limiter import acquire


async def request(config_dir: Path, method: str, path: str, payload: Any = None):
    key = credential(config_dir)
    if not key:
        raise ValueError("credentials_missing")
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.request(
            method,
            "https://api.typesafe.ai/v1" + path,
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()


class DirectInference:
    """The inference calls themselves."""

    async def agno(self, person_id: str, call: AgnoCall) -> AgnoAnswer:
        """Run ``call`` on the model of ``person_id``'s slot ``call.slot``.

        A restricted model is not given the description: the brain folded it
        into the message, with the output schema, which it then sends none of.
        """
        logger = get_logger()
        config = get_model_mapping(person_id)[call.slot]
        overlay = effort_settings(config.effort, call.effort, logger=logger)
        parameters = deepcopy(config.parameters)
        parameters.update(deepcopy(overlay))
        restricted = config.is_restricted_model
        agent = Agent(
            name=call.brain,
            model=instantiate_class(
                config.model_class, expected_type=Model, **parameters
            ),
            description=None if restricted else call.description,
            output_schema=(
                _output_class(call.output_schema) if call.output_schema else None
            ),
            tool_call_limit=5,
            session_state=call.session_state,
        )
        if config.rate_limit and config.rate_limit.max_requests_per_minute:
            await acquire(config.name, config.rate_limit.max_requests_per_minute)
        # The request is made with this id, so it is the effective model whether
        # or not the call succeeds. The effort level is effective only when it
        # actually contributed settings.
        model = str(parameters.get("id", "") or "")
        effort = call.effort.resolved if overlay else ""

        with bind_span(call.span) if call.span else nullcontext():
            started = time.monotonic()

            def summary(status: str, usage: dict[str, Any] | None = None) -> None:
                # A definition that names no model id of its own leaves the span's
                # model empty -- the request ran on the provider's default, which
                # is unknown -- while ``model.slot`` still names the slot.
                record_summary(
                    logger,
                    "llm",
                    config.name,
                    status,
                    started=started,
                    attributes={"model.slot": config.name},
                    model=model,
                    effort=effort,
                    usage=usage,
                )

            record_correlated_io(
                io_type="llm.request",
                payload={
                    "effort": effort_diagnostics(call.effort, overlay, model=model),
                    "person_id": person_id,
                    "brain": call.brain,
                    "model": config.name,
                    "model_class": config.model_class,
                    "restricted_model": restricted,
                    "response_class": str((call.output_schema or {}).get("title", "")),
                    "description": call.description,
                    "message": call.message,
                    "session_state": call.session_state,
                },
            )
            try:
                response = await agent.arun(call.message)
            except BaseException:
                # Cancelled too: the window gives the call up after its time.
                summary("failed")
                raise
            content = response.content
            record_correlated_io(
                io_type="llm.response",
                payload={
                    "person_id": person_id,
                    "brain": call.brain,
                    "model": config.name,
                    "content": content,
                },
            )
            usage = _response_usage(response)
            summary("finished", usage)
        if isinstance(content, BaseModel):
            content = content.model_dump(mode="json")
        return AgnoAnswer(content=content, model=model, usage=usage)

    async def jev(self, call: JevCall) -> dict[str, Any]:
        """Ask Jev with the workspace's key."""
        return dict(
            await request(
                get_workspace_config_dir(), "POST", "/systemone", call.model_dump()
            )
        )


def _output_class(schema: dict[str, Any]) -> type[BaseModel]:
    """A model the provider is shown ``schema`` by, exactly as the brain that
    asked wrote it, and that takes any object back: that brain validates it.

    Every provider reads the schema of a model from its ``model_json_schema()``;
    agno's own JSON form is each provider's wire format instead. The method is
    replaced rather than the generated schema amended, which would have
    pydantic resolve the ``$ref`` of nested models this class does not have.
    """

    class Output(BaseModel):
        model_config = ConfigDict(extra="allow")

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return deepcopy(schema)

    # Providers name the schema after the class, in these characters only.
    name = re.sub(r"[^A-Za-z0-9_-]", "_", str(schema.get("title") or ""))[:64]
    Output.__name__ = Output.__qualname__ = name or "Output"
    return Output


def _response_usage(response: object) -> dict[str, Any]:
    """Token counters of a run, as a plain dict.

    ``RunOutput.metrics`` is a dataclass that drops zero and empty entries in
    ``to_dict()``, so the span only carries counters the provider reported.
    """
    metrics = getattr(response, "metrics", None)
    to_dict = getattr(metrics, "to_dict", None)
    if callable(to_dict):
        return {str(key): value for key, value in to_dict().items()}
    if isinstance(metrics, dict):
        return {str(key): value for key, value in metrics.items()}
    return {}
