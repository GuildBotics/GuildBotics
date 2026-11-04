"""Inference runs in the guest; leased gateway credentials stay on the host."""

from copy import deepcopy
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from guildbotics.guest.host_client import HostClient
from guildbotics.intelligences.agent_runtime.wire import (
    IoEntry,
    ResolvedGuest,
    SummaryEntry,
)
from guildbotics.intelligences.brains.agno_agent import ModelConfig
from guildbotics.intelligences.brains.inference import (
    AgnoAnswer,
    AgnoCall,
    InferenceFailure,
    JevCall,
)
from guildbotics.intelligences.effort import effort_settings
from guildbotics.intelligences.llm_providers import classify_failure
from guildbotics.utils.import_utils import load_class

PROVIDERS = {
    "agno.models.openai.OpenAIChat": "openai",
    "agno.models.anthropic.Claude": "anthropic",
    "agno.models.google.Gemini": "gemini",
}


class GuestInference:
    """Use only the host-resolved model and the command's leased gateway."""

    def __init__(self, data: ResolvedGuest, client: HostClient):
        self.data = data
        self.client = client

    async def agno(self, person_id: str, call: AgnoCall) -> AgnoAnswer:
        from agno.agent import Agent
        from agno.run.base import RunStatus

        if person_id != self.data.member["person_id"]:
            raise ValueError("Inference is limited to the active member.")
        definition = next(
            (
                s.definition
                for s in self.data.brains.values()
                if s.kind == "model" and s.slot == call.slot
            ),
            None,
        )
        if definition is None:
            raise ValueError("Unknown resolved model slot")
        config = ModelConfig.model_validate(definition)
        provider = PROVIDERS.get(config.model_class)
        if provider is None:
            raise ValueError("Unsupported inference model_class")
        params = deepcopy(config.parameters)
        params.update(effort_settings(config.effort, call.effort))
        params["api_key"] = self.data.inference_keys[provider]
        url = self.data.inference_urls[provider]
        if provider == "gemini":
            params["client_params"] = {"http_options": {"base_url": url}}
        elif provider == "anthropic":
            params["client_params"] = {"base_url": url}
        else:
            params["base_url"] = url
        model = load_class(config.model_class)(**params)
        failures: list[Exception] = []
        invoke = getattr(model, "ainvoke", None)
        if callable(invoke):

            async def tracked_invoke(*args: Any, **kwargs: Any):
                failures.clear()
                try:
                    return await invoke(*args, **kwargs)
                except Exception as error:
                    failures.append(error)
                    raise

            model.ainvoke = tracked_invoke
        await self.client.acall("acquire_inference", model_name=config.name)
        agent = Agent(
            name=call.brain,
            model=model,
            description=None if config.is_restricted_model else call.description,
            output_schema=_output_class(call.output_schema)
            if call.output_schema
            else None,
            tool_call_limit=5,
            session_state=call.session_state,
        )
        await self.client.record(
            [
                IoEntry(
                    span=call.span,
                    io_type="llm.request",
                    payload={
                        "brain": call.brain,
                        "slot": call.slot,
                        "message": call.message,
                        "description": call.description,
                    },
                )
            ]
        )
        try:
            response = await agent.arun(call.message)
            if response.status != RunStatus.completed:
                if failures:
                    raise failures[-1]
                raise RuntimeError(
                    "The inference provider did not complete the request."
                )
            content = response.content
            if isinstance(content, BaseModel):
                content = content.model_dump(mode="json")
            usage = getattr(response, "metrics", None)
            usage = (
                usage.to_dict()
                if usage is not None and hasattr(usage, "to_dict")
                else {}
            )
            answer = AgnoAnswer(
                content=content, model=str(params.get("id", "")), usage=usage
            )
            await self.client.record(
                [
                    IoEntry(
                        span=call.span,
                        io_type="llm.response",
                        payload=answer.model_dump(mode="json"),
                    ),
                    SummaryEntry(
                        span=call.span,
                        slot=call.slot,
                        tool=provider,
                        model_specified=bool(answer.model),
                        status="finished",
                        model=answer.model,
                        usage={k: v for k, v in usage.items() if isinstance(v, int)},
                    ),
                ]
            )
            return answer
        except Exception as exc:
            await self.client.record(
                [
                    SummaryEntry(
                        span=call.span,
                        slot=call.slot,
                        tool=provider,
                        model_specified=bool(params.get("id")),
                        status="failed",
                    )
                ]
            )
            failure = InferenceFailure(exc)
            failure.category = classify_failure(provider, failure)
            raise failure from exc

    async def jev(self, call: JevCall) -> dict[str, Any]:
        await self.client.record(
            [
                IoEntry(
                    span=call.span,
                    io_type="jev.request",
                    payload=call.model_dump(mode="json"),
                )
            ]
        )
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    self.data.inference_urls["jev"].rstrip("/") + "/systemone",
                    headers={
                        "Authorization": "Bearer " + self.data.inference_keys["jev"]
                    },
                    json={
                        "state": call.state,
                        "questions": call.questions,
                        "model": call.model,
                    },
                )
                response.raise_for_status()
                answer = dict(response.json())
            await self.client.record(
                [
                    IoEntry(span=call.span, io_type="jev.response", payload=answer),
                    SummaryEntry(
                        span=call.span,
                        slot=call.model,
                        tool="jev",
                        model_specified=True,
                        status="finished",
                        model=str(answer.get("model", call.model)),
                        usage={
                            k: v
                            for k, v in answer.get("usage", {}).items()
                            if isinstance(v, int)
                        },
                    ),
                ]
            )
            return answer
        except Exception as exc:
            await self.client.record(
                [
                    SummaryEntry(
                        span=call.span,
                        slot=call.model,
                        tool="jev",
                        model_specified=True,
                        status="failed",
                    )
                ]
            )
            failure = InferenceFailure(exc)
            failure.category = classify_failure("jev", failure)
            raise failure from exc


def _output_class(schema: dict[str, Any]) -> type[BaseModel]:
    class Output(BaseModel):
        model_config = ConfigDict(extra="allow")

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return schema

    return Output
