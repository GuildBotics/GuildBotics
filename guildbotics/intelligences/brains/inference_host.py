"""The external inference calls, made on the host where their keys are.

A brain asks for them through :func:`~guildbotics.intelligences.brains.
inference.inference`: on the host, here directly; inside a command's isolated
environment, through the command's window, whose grant answers it here. Either
way what is called is settled here from the host's own settings -- the model
of the member's slot, its effort mapping, its rate limit -- and the call is
recorded here, under the span the brain opened: the span's end names the
provider whose key was used and, when the call failed, why the provider
refused it and what it answered, so the latest outcome per provider is read
from the records. The answer is recorded with the key the call used and the
workspace's secrets masked: the records are read inside the environment of a
command that inspects them.
"""

from __future__ import annotations

import re
import time
from contextlib import nullcontext
from copy import deepcopy
from typing import Any

import httpx
from agno.agent import Agent
from agno.models.base import Model
from agno.run.base import RunStatus
from pydantic import BaseModel, ConfigDict

from guildbotics.intelligences.brains.agno_agent import get_model_mapping
from guildbotics.intelligences.brains.inference import (
    AgnoAnswer,
    AgnoCall,
    InferenceFailure,
    JevCall,
)
from guildbotics.intelligences.brains.jev import JEV_PROVIDER, credential
from guildbotics.intelligences.brains.span_summary import record_summary
from guildbotics.intelligences.effort import effort_diagnostics, effort_settings
from guildbotics.intelligences.llm_providers import classify_failure, provider_of
from guildbotics.observability import bind_span
from guildbotics.observability.diagnostics_events import record_correlated_io
from guildbotics.utils.fileio import get_workspace_config_dir
from guildbotics.utils.import_utils import instantiate_class
from guildbotics.utils.log_utils import get_logger
from guildbotics.utils.rate_limiter import acquire
from guildbotics.utils.shared_redaction import redact_for_sharing


async def request(key: str, method: str, path: str, payload: Any = None):
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
        provider = provider_of(config.name)
        model_instance = instantiate_class(
            config.model_class, expected_type=Model, **parameters
        )
        failures = _failures_of(model_instance)
        agent = Agent(
            name=call.brain,
            model=model_instance,
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

            def summary(
                status: str,
                usage: dict[str, Any] | None = None,
                failure: InferenceFailure | None = None,
                key: str = "",
            ) -> None:
                # A definition that names no model id of its own leaves the span's
                # model empty -- the request ran on the provider's default, which
                # is unknown -- while ``model.slot`` still names the slot.
                record_summary(
                    logger,
                    "llm",
                    config.name,
                    status,
                    duration_ms=(time.monotonic() - started) * 1000,
                    attributes={
                        "model.slot": config.name,
                        **_credential_attributes("llm", provider, failure, key),
                    },
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
            if response.status != RunStatus.completed:
                # agno keeps a failed or cancelled run and answers with what it
                # has. Only an error the provider raised tells why it refused.
                failure = _classified(
                    provider, failures[-1] if failures else RuntimeError()
                )
                summary(
                    "failed",
                    failure=failure if failures else None,
                    key=str(getattr(model_instance, "api_key", None) or ""),
                )
                raise failure
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
        """Ask Jev with the workspace's key, under the span the brain opened.

        A workspace without the key asks nothing, so it records no call.
        """
        key = credential(get_workspace_config_dir())
        if not key:
            raise ValueError("credentials_missing")
        with bind_span(call.span) if call.span else nullcontext():
            started = time.monotonic()

            def summary(
                status: str,
                result: dict[str, Any] | None = None,
                failure: InferenceFailure | None = None,
            ) -> None:
                record_summary(
                    get_logger(),
                    JEV_PROVIDER,
                    call.model,
                    status,
                    duration_ms=(time.monotonic() - started) * 1000,
                    attributes=_credential_attributes(JEV_PROVIDER, "", failure, key),
                    model=str((result or {}).get("model") or ""),
                    usage=(result or {}).get("usage"),
                )

            try:
                result = dict(
                    await request(
                        key,
                        "POST",
                        "/systemone",
                        call.model_dump(exclude={"span"}),
                    )
                )
            except Exception as exc:
                failure = _classified(JEV_PROVIDER, exc)
                summary("failed", failure=failure)
                raise failure from exc
            except BaseException:
                summary("failed")
                raise
            summary("finished", result)
        return result


def _failures_of(model: Model) -> list[Exception]:
    """The error ``model``'s latest call failed with, if it failed.

    agno's run catches it and keeps only its text, as the answer, so the
    provider's error -- what tells why it refused -- is taken where it is
    raised, on this instance alone. A later call that succeeds clears it.
    """
    failures: list[Exception] = []
    invoke = model.ainvoke

    async def ainvoke(*args: Any, **kwargs: Any) -> Any:
        failures.clear()
        try:
            return await invoke(*args, **kwargs)
        except Exception as exc:
            failures.append(exc)
            raise

    model.ainvoke = ainvoke  # type: ignore[method-assign]  # this instance only
    return failures


def _classified(provider: str, exc: BaseException) -> InferenceFailure:
    failure = InferenceFailure(exc)
    failure.category = classify_failure(provider, failure)
    return failure


def _credential_attributes(
    service: str, provider: str, failure: InferenceFailure | None, key: str = ""
) -> dict[str, Any]:
    """What a span's end says of the key it used: the service (``llm`` or
    ``jev``), the LLM provider, and why it was refused, with what the
    provider answered -- ``key`` and the workspace's secrets masked."""
    attributes: dict[str, Any] = {"credential.provider": service}
    if provider:
        attributes["llm.provider"] = provider
    if failure:
        response = failure.response.replace(key, "***") if key else failure.response
        attributes["error.category"] = failure.category
        attributes["error.response"] = redact_for_sharing(response)
        if failure.status_code:
            attributes["error.status_code"] = failure.status_code
    return attributes


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
