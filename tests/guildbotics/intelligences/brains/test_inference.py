"""The host's own callers of a brain reach the model through the same
``run()`` a command's brain does, the call made directly on the host."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from guildbotics.editions.simple.simple_brain_factory import SimpleBrainFactory
from guildbotics.intelligences import functions
from guildbotics.intelligences.brains import (
    agno_agent,
    inference_host,
    jev,
    span_summary,
)
from guildbotics.intelligences.brains.inference import inference
from guildbotics.intelligences.brains.inference_host import DirectInference
from guildbotics.intelligences.decisions import engines
from guildbotics.intelligences.decisions.chat_policy import QUESTIONS
from guildbotics.intelligences.decisions.models import DecisionConfig
from tests.conftest import FakeContext


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["agno", "jev"])
@pytest.mark.parametrize("category", ["failed", "refused", "unavailable"])
@pytest.mark.parametrize(
    ("message", "details"),
    [
        ("The host call timed out.", {}),
        ("The call's result is too large.", {}),
        ("Host operation failed.", {}),
        ("The inference call failed (ProviderError).", {"error_type": "ProviderError"}),
        (
            "The inference call failed (ProviderError, reported status 429).",
            {"error_type": "ProviderError", "status_code": 429},
        ),
    ],
)
async def test_window_inference_failure_contract(method, category, message, details):
    from unittest.mock import AsyncMock

    from guildbotics.commands.errors import CommandError
    from guildbotics.intelligences.agent_runtime.host_client import HostCallError
    from guildbotics.intelligences.brains.inference import AgnoCall, JevCall, _Window
    from guildbotics.intelligences.effort import ResolvedEffort

    error = HostCallError(category, message, details)
    window = _Window(SimpleNamespace(acall=AsyncMock(side_effect=error)))
    call = (
        window.agno(
            "aiko",
            AgnoCall(
                brain="test",
                slot="default",
                effort=ResolvedEffort(),
                description="",
                message="hi",
            ),
        )
        if method == "agno"
        else window.jev(JevCall(state={}, questions={}, model="jev-latest"))
    )
    with pytest.raises(
        CommandError if category == "failed" else HostCallError
    ) as caught:
        await call
    if category == "failed":
        assert str(caught.value) == message
        assert caught.value.__cause__ is error
    else:
        assert caught.value is error


class _Model:
    """The model of every slot of ``person_id``: the answers a test scripts,
    and what it was asked."""

    def __init__(
        self,
        monkeypatch,
        *answers: Any,
        person_id: str = "p1",
        restricted: bool = False,
    ) -> None:
        self.answers = list(answers)
        self.asked: list[dict[str, Any]] = []
        model = self

        class Agent:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def arun(self, message: str) -> Any:
                model.asked.append({"message": message, **self.kwargs})
                answer = model.answers.pop(0)
                if isinstance(answer, BaseException):
                    raise answer
                schema = self.kwargs.get("output_schema")
                if schema is not None:
                    # What agno shows the provider.
                    model.asked[-1]["shown"] = schema.model_json_schema()
                # agno reads a structured answer into the output schema.
                if schema is not None and isinstance(answer, dict):
                    answer = schema.model_validate(answer)
                return SimpleNamespace(content=answer, metrics=None)

        monkeypatch.setattr(inference_host, "Agent", Agent)
        monkeypatch.setattr(
            inference_host, "instantiate_class", lambda *args, **kwargs: object()
        )
        monkeypatch.setitem(
            agno_agent.person_model_mapping,
            person_id,
            {
                "default": agno_agent.ModelConfig(
                    name="models/test.yml",
                    model_class="tests.FakeModel",
                    parameters={"id": "test-model"},
                    is_restricted_model=restricted,
                )
            },
        )


class _Context(FakeContext):
    """A member's context whose brains are the configured ones."""

    language_name = "English"

    def get_brain(self, name: str, config: Any, class_resolver: Any) -> Any:
        return SimpleBrainFactory().create_brain(
            self.person.person_id, name, "en", self.logger, config, class_resolver
        )


def test_the_host_calls_the_apis_itself() -> None:
    assert isinstance(inference(), DirectInference)


@pytest.mark.asyncio
async def test_talk_as_is_answered_through_the_brain(monkeypatch) -> None:
    """``talk_as`` -- the ticket selector's failure comments and the live LLM
    diagnostics -- reads the model's structured answer as before."""
    model = _Model(
        monkeypatch, {"content": " Hello! ", "author": "Tester", "author_type": "AI"}
    )

    said = await functions.talk_as(_Context(), "Say hello.", "Ticket", [])

    assert said == "Hello!"
    (asked,) = model.asked
    assert "Say hello." in asked["description"]
    assert asked["output_schema"].model_json_schema()["title"] == "MessageResponse"
    assert asked["session_state"]["topic"] == "Say hello."
    assert "context" not in asked["session_state"]


@pytest.mark.asyncio
async def test_the_chat_decision_is_evaluated_through_the_brain(monkeypatch) -> None:
    answers = {
        key: {"type": q.type, "value": "none" if q.type == "choice" else "false"}
        for key, q in QUESTIONS.items()
    }
    model = _Model(monkeypatch, json.dumps({"answers": answers}))
    context = _Context()

    result = await engines.evaluate(
        DecisionConfig(),
        {"text": "hello"},
        QUESTIONS,
        person_id="p1",
        logger=logging.getLogger("test"),
        brain_factory=SimpleNamespace(
            create_brain=lambda _person, name, _language, _logger, config: (
                context.get_brain(name, config, None)
            )
        ),
    )

    assert not result.error
    assert set(result.answers) == set(QUESTIONS)
    assert result.model == "test-model"
    assert json.loads(model.asked[0]["message"])["state"] == {"text": "hello"}


@pytest.mark.asyncio
async def test_jev_is_asked_with_the_workspace_key(monkeypatch) -> None:
    asked: list[Any] = []

    async def request(_root, method, path, payload):
        asked.append((method, path, payload))
        return {"model": "jev-1", "answers": {}, "usage": {"input_tokens": 3}}

    monkeypatch.setattr(inference_host, "request", request)
    brain = jev.JevBrain("p1", "chat_decision", logging.getLogger("test"))

    result = await brain.run(json.dumps({"state": {"a": 1}, "questions": {}}))

    assert result["model"] == "jev-1"
    assert asked == [
        (
            "POST",
            "/systemone",
            {"state": {"a": 1}, "questions": {}, "model": "jev-latest"},
        )
    ]
    assert brain.execution.usage == {"input_tokens": 3}


@pytest.mark.asyncio
async def test_a_restricted_model_is_asked_in_the_message_alone(monkeypatch) -> None:
    """Description and output schema are folded into the message, and the
    plain answer is read as the response class."""
    model = _Model(
        monkeypatch,
        '{"content": "Hi", "author": "Tester", "author_type": "AI"}',
        restricted=True,
    )

    said = await functions.talk_as(_Context(), "Say hi.", "", [])

    assert said == "Hi"
    (asked,) = model.asked
    assert asked["description"] is None
    assert asked["output_schema"] is None
    assert "Say hi." in asked["message"]
    assert "<MessageResponse Schema>" in asked["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_class",
    [
        "guildbotics.intelligences.common.MessageResponse",
        "guildbotics.intelligences.common.DecisionResponseList",
        "guildbotics.commands.authoring.CommandAuthoringResult",
    ],
)
async def test_the_provider_is_shown_the_schema_of_the_response_class(
    monkeypatch, response_class
) -> None:
    """Nested models included, whose schema refers to its own definitions."""
    model = _Model(monkeypatch, "{}")
    brain = _Context().get_brain(
        "functions/answer", {"body": "Answer.", "response_class": response_class}, None
    )

    await brain.run("hello")

    assert model.asked[0]["shown"] == brain.response_class.model_json_schema()


@pytest.mark.asyncio
async def test_a_call_given_up_on_still_ends_its_span(monkeypatch) -> None:
    """The window cancels a call that outlasts its time."""
    _Model(monkeypatch, asyncio.CancelledError())
    ended: list[str] = []
    monkeypatch.setattr(
        span_summary,
        "record_span_summary",
        lambda **kwargs: ended.append(kwargs["status"]),
    )
    brain = _Context().get_brain("functions/answer", {"body": "Answer."}, None)

    with pytest.raises(asyncio.CancelledError):
        await brain.run("hello")

    assert ended == ["failed"]
