"""Frozen host inference contracts exercised at the current guest boundary."""

import asyncio
import inspect
import logging
from copy import deepcopy
from types import SimpleNamespace

import agno.agent
import httpx
import pytest
from pydantic import ValidationError
from agno.run.base import RunStatus
from pydantic import BaseModel

from guildbotics.guest import inference as module
from guildbotics.guest.resolved import ResolvedBrainFactory
from guildbotics.intelligences.brains import inference as installed
from guildbotics.intelligences.brains.inference import InferenceFailure, JevCall
from guildbotics.intelligences.effort import EffortError
from tests.guildbotics.guest.test_resolved_inference import data


@pytest.fixture
def harness(monkeypatch):
    resolved = data()
    resolved.inference_urls = {"openai": "http://gateway/v1", "jev": "http://gateway"}
    resolved.inference_keys = {"openai": "placeholder", "jev": "placeholder"}
    captured = {"records": [], "status": RunStatus.completed, "response": "answer"}
    real_agent = agno.agent.Agent

    class Model:
        def __init__(self, **kwargs):
            captured["parameters"] = kwargs

        async def ainvoke(self):
            error = captured.get("provider_error")
            if error:
                raise error

    class Agent:
        def __init__(self, **kwargs):
            inspect.signature(real_agent.__init__).bind_partial(None, **kwargs)
            captured["agent"] = kwargs
            self.model = kwargs["model"]

        async def arun(self, message):
            captured["message"] = message
            error = captured.get("error")
            if error:
                raise error
            if captured.get("provider_error"):
                try:
                    await self.model.ainvoke()
                except Exception:
                    pass
                if captured.get("provider_retry_succeeds"):
                    captured.pop("provider_error")
                    await self.model.ainvoke()
            return SimpleNamespace(
                status=captured["status"], content=captured["response"], metrics=None
            )

    class Window:
        async def acall(self, name, **kwargs):
            assert name == "acquire_inference"

        async def record(self, entries):
            captured["records"].extend(entries)

    monkeypatch.setattr(module, "load_class", lambda _: Model)
    monkeypatch.setattr(agno.agent, "Agent", Agent)
    implementation = module.GuestInference(resolved, Window())
    monkeypatch.setattr(installed, "_installed", implementation)

    def brain(config=None):
        return ResolvedBrainFactory(resolved).create_brain(
            "aiko",
            "reply",
            "en",
            logging.getLogger("test"),
            config={"body": "instruction", **(config or {})},
        )

    return resolved, captured, brain, implementation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overlay,runtime,frontmatter,expected,effort",
    [
        (
            {"high": {"reasoning_effort": "high"}},
            {},
            "high",
            {"id": "fixture", "reasoning_effort": "high"},
            "high",
        ),
        ({"high": {"id": "stronger"}}, {}, "high", {"id": "stronger"}, "high"),
        (
            {"high": {"thinking": {"budget_tokens": 8000}}},
            {},
            "high",
            {"id": "fixture", "thinking": {"budget_tokens": 8000}},
            "high",
        ),
        (
            {"high": {"id": "stronger"}, "low": {"id": "faster"}},
            {"effort": "low"},
            "high",
            {"id": "faster"},
            "low",
        ),
        (
            {"high": {"id": "stronger"}},
            {"effort": "default"},
            "high",
            {"id": "fixture"},
            "",
        ),
        ({}, {}, "high", {"id": "fixture"}, ""),
        ({}, {}, "", {"id": "fixture"}, ""),
    ],
)
async def test_effort_overlays_parameters_diagnostics_and_effective_summary(
    harness, overlay, runtime, frontmatter, expected, effort
):
    resolved, captured, brain, _ = harness
    resolved.brains["default"].definition["effort"] = overlay
    await brain({"effort": frontmatter}).run("question", session_state=runtime)
    parameters = captured["parameters"].copy()
    assert parameters.pop("api_key") == "placeholder"
    assert parameters.pop("base_url") == "http://gateway/v1"
    assert parameters == expected
    summary = captured["records"][-1]
    assert (summary.model, summary.effort, summary.status, summary.tool) == (
        expected["id"],
        effort,
        "finished",
        "openai",
    )
    request = captured["records"][0].payload["effort"]
    assert request["applied_keys"] == sorted(
        overlay.get(runtime.get("effort", frontmatter), {})
    )
    assert request["unsupported"] == bool(frontmatter == "high" and not overlay)


@pytest.mark.asyncio
async def test_diagnostics_do_not_record_effort_values_and_missing_id_stays_empty(
    harness,
):
    resolved, captured, brain, _ = harness
    definition = resolved.brains["default"].definition
    definition["parameters"] = {"temperature": 0.2}
    definition["effort"] = {"high": {"api_key": "synthetic-secret"}}
    await brain({"effort": "high"}).run("question")
    assert captured["records"][-1].model == ""
    assert captured["records"][-1].model_specified is False
    assert "synthetic-secret" not in str(captured["records"])
    assert captured["records"][0].payload["effort"]["applied_keys"] == ["api_key"]


@pytest.mark.asyncio
@pytest.mark.parametrize("restricted", [False, True])
async def test_installed_agent_kwargs_response_schema_and_session_state(
    harness, restricted
):
    class Reply(BaseModel):
        text: str

    class Uncopyable:
        def __deepcopy__(self, memo):
            raise TypeError("cannot copy context")

    resolved, captured, brain, _ = harness
    resolved.brains["default"].definition["is_restricted_model"] = restricted
    captured["response"] = '{"text":"answer"}'
    configured = brain()
    configured.response_class = Reply
    answer = await configured.run(
        "question", session_state={"context": Uncopyable(), "topic": "test"}
    )
    assert answer == Reply(text="answer")
    agent = captured["agent"]
    assert deepcopy(agent["session_state"]) == {"topic": "test"}
    assert "response_model" not in agent
    if restricted:
        assert agent["description"] is None
        assert agent["output_schema"] is None
        assert "instruction" in captured["message"]
        assert "Reply Schema" in captured["message"]
    else:
        assert agent["description"] == "instruction"
        assert agent["output_schema"].model_json_schema() == Reply.model_json_schema()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RunStatus.cancelled, RunStatus.error])
@pytest.mark.parametrize("retried", [False, True])
async def test_incomplete_provider_run_fails_without_a_refusal(
    harness, status, retried
):
    _, captured, brain, _ = harness
    captured["status"] = status
    if retried:
        captured["provider_error"] = RuntimeError("first call failed")
        captured["provider_retry_succeeds"] = True
    with pytest.raises(InferenceFailure) as error:
        await brain().run("question")
    assert error.value.error_type == "RuntimeError"
    assert error.value.category == "other"
    assert captured["records"][-1].status == "failed"


@pytest.mark.asyncio
async def test_cancellation_still_records_a_failed_summary(harness):
    _, captured, brain, _ = harness
    captured["error"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await brain().run("question")
    assert captured["records"][-1].type == "summary"
    assert captured["records"][-1].status == "failed"


@pytest.mark.asyncio
async def test_provider_refusal_preserves_category_without_secret_error_text(harness):
    import openai

    _, captured, brain, _ = harness
    captured["status"] = RunStatus.error
    response = httpx.Response(429, request=httpx.Request("POST", "http://gateway"))
    captured["provider_error"] = openai.RateLimitError(
        "synthetic-secret", response=response, body={"type": "insufficient_quota"}
    )
    with pytest.raises(InferenceFailure) as error:
        await brain().run("question")
    assert (error.value.error_type, error.value.status_code, error.value.category) == (
        "RateLimitError",
        429,
        "credit",
    )
    assert "synthetic-secret" not in str(error.value)
    assert captured["records"][-1].status == "failed"


@pytest.mark.parametrize("effort", ["", "low", "high", "extreme"])
def test_resolved_factory_keeps_frontmatter_effort_and_model_slot(harness, effort):
    _, _, brain, _ = harness
    if effort == "extreme":
        with pytest.raises(EffortError):
            brain({"effort": effort})
    else:
        configured = brain({"effort": effort})
        assert (configured.effort, configured.model_slot) == (effort, "main")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 401, "cancelled"])
async def test_jev_uses_leased_gateway_and_records_success_or_refusal(
    harness, monkeypatch, status
):
    _, captured, _, implementation = harness
    real_client = httpx.AsyncClient

    def respond(request):
        assert request.url == "http://gateway/systemone"
        assert request.headers["authorization"] == "Bearer placeholder"
        import json

        assert json.loads(request.content) == {
            "state": {"a": 1},
            "questions": {},
            "model": "jev-latest",
        }
        if status == "cancelled":
            raise asyncio.CancelledError()
        return httpx.Response(
            status, json={"model": "jev-1", "answers": {}, "usage": {"input_tokens": 3}}
        )

    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    call = JevCall(state={"a": 1}, questions={}, model="jev-latest")
    if status == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await implementation.jev(call)
        assert captured["records"][-1].status == "failed"
    elif status == 401:
        with pytest.raises(InferenceFailure) as error:
            await implementation.jev(call)
        assert error.value.category == "authentication"
        assert captured["records"][-1].status == "failed"
    else:
        assert (await implementation.jev(call))["model"] == "jev-1"
        assert captured["records"][-1].usage == {"input_tokens": 3}


@pytest.mark.asyncio
async def test_talk_as_keeps_structured_brain_response(harness):
    from guildbotics.intelligences.functions import talk_as

    _, captured, brain, _ = harness
    captured["response"] = '{"content":" Hello! ","author":"Aiko","author_type":"AI"}'
    context = SimpleNamespace(
        get_brain=lambda *_: brain(
            {
                "body": "Say hello: {{ topic }}",
                "response_class": "guildbotics.intelligences.common.MessageResponse",
            }
        )
    )
    assert await talk_as(context, "Say hello.", "Ticket", []) == "Hello!"
    assert (
        captured["agent"]["output_schema"].model_json_schema()["title"]
        == "MessageResponse"
    )
    assert captured["agent"]["session_state"]["topic"] == "Say hello."
    assert "context" not in captured["agent"]["session_state"]


@pytest.mark.asyncio
async def test_missing_jev_lease_records_no_provider_call(harness):
    resolved, captured, _, implementation = harness
    resolved.inference_keys.pop("jev")
    with pytest.raises(ValueError, match="Jev gateway"):
        await implementation.jev(JevCall(state={}, questions={}, model="jev-latest"))
    assert captured["records"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_class",
    [
        "guildbotics.intelligences.common.MessageResponse",
        "guildbotics.commands.authoring.CommandAuthoringResult",
    ],
)
async def test_nested_production_response_schema_reaches_the_provider(
    harness, response_class
):
    _, captured, brain, _ = harness
    configured = brain({"response_class": response_class})
    # The output schema itself is exercised; parsing the response happens in the brain.
    captured["response"] = "{}"
    try:
        await configured.run("question")
    except ValidationError:
        # Empty content can be invalid; the schema sent to the provider is the contract.
        pass
    assert (
        captured["agent"]["output_schema"].model_json_schema()
        == configured.response_class.model_json_schema()
    )
