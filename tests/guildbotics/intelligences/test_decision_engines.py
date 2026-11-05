"""Frozen judgment engine contracts remain shared Python inside the command VM."""

import json
import logging
from types import SimpleNamespace

import pytest

from guildbotics.intelligences.brains import jev
from guildbotics.intelligences.decisions import assessment, engines
from guildbotics.intelligences.decisions.chat_policy import QUESTIONS
from guildbotics.intelligences.decisions.models import DecisionConfig
from guildbotics.runtime.brain import Brain, ExecutionMetadata, public_parameters
from tests.guildbotics.intelligences.test_decision_rules import answers
from tests.guildbotics.intelligences.test_decision_assessment import Recorder


@pytest.mark.parametrize(
    "settings",
    [
        {"thinking_budget": 8000},
        {"thinking": {"type": "enabled", "budget_tokens": 8000}},
    ],
)
def test_public_provider_thinking_settings_exclude_credentials(settings):
    parameters = {**settings, "api_key": "private-inline-key"}
    if "thinking" in parameters:
        parameters["thinking"] = {
            **parameters["thinking"],
            "headers": {"Authorization": "private-inline-key"},
        }
    assert public_parameters(parameters) == settings


@pytest.mark.asyncio
async def test_jev_uses_latest_and_records_the_returned_version(monkeypatch):
    calls = []

    async def request(call):
        calls.append(call)
        assert call.model == "jev-latest"
        return {
            "model": "jev-future-test-version",
            "answers": {},
            "usage": {"input_tokens": 12},
        }

    monkeypatch.setattr(jev, "inference", lambda: SimpleNamespace(jev=request))
    brain = jev.JevBrain("alice", "chat_decision", logging.getLogger())
    await brain.run(json.dumps({"state": "test", "questions": {}}))
    assert len(calls) == 1
    assert brain.execution.model == "jev-future-test-version"
    assert brain.execution.usage == {"input_tokens": 12}


@pytest.mark.parametrize("model", ["jev-1.13.0", "jev-preview"])
def test_jev_rejects_other_models(model):
    with pytest.raises(ValueError, match="Unsupported Jev model"):
        jev.JevBrain("alice", "chat_decision", logging.getLogger(), model=model)


@pytest.mark.asyncio
async def test_jev_one_request_and_failure_is_sanitized(monkeypatch):
    calls = []

    async def request(call):
        calls.append(call)
        raise RuntimeError("private-test-key")

    monkeypatch.setattr(jev, "inference", lambda: SimpleNamespace(jev=request))
    brain = jev.JevBrain("alice", "chat_decision", logging.getLogger())
    result = await engines.evaluate(
        DecisionConfig(),
        {"text": "依頼"},
        QUESTIONS,
        person_id="alice",
        logger=logging.getLogger(),
        brain_factory=SimpleNamespace(create_brain=lambda *args, **kwargs: brain),
    )
    assert len(calls) == 1
    assert set(calls[0].questions) == set(QUESTIONS)
    assert "private-test-key" not in result.model_dump_json()
    assert result.error


@pytest.mark.asyncio
async def test_common_factory_run_and_execution_metadata():
    state = {"text": "この対応は取り消します"}
    calls = []

    class TestBrain(Brain):
        async def run(self, message, **kwargs):
            calls.append((json.loads(message), kwargs))
            self.execution = ExecutionMetadata(
                model="resolved-model", usage={"input_tokens": 12}
            )
            return json.dumps(
                {
                    "answers": {
                        key: {
                            "type": q.type,
                            "value": "none" if q.type == "choice" else "false",
                        }
                        for key, q in QUESTIONS.items()
                    }
                }
            )

    def create(person_id, name, language, logger, config):
        assert person_id == "alice"
        assert set(config) == {"brain", "body"}
        assert config["brain"] == "custom_judgment"
        assert "only the supplied state" in config["body"]
        return TestBrain(person_id, name, logger)

    result = await engines.evaluate(
        DecisionConfig(brain="custom_judgment"),
        state,
        QUESTIONS,
        person_id="alice",
        logger=logging.getLogger(),
        brain_factory=SimpleNamespace(create_brain=create),
    )
    assert not result.error
    assert len(calls) == 1
    request, kwargs = calls[0]
    assert request["state"] == state and set(request["questions"]) == set(QUESTIONS)
    assert not kwargs
    assert result.model == "resolved-model" and result.usage == {"input_tokens": 12}
    assert result.retries is None


@pytest.mark.asyncio
@pytest.mark.parametrize("recording_fails", [False, True])
async def test_recorded_response_effort_is_separate_from_judgment_settings(
    monkeypatch, recording_fails
):
    recorder = Recorder()
    calls = 0
    recorded = []

    async def evaluate(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = answers(pending_request="true", work_files="true")
        result.configuration = {"model": "judge-model", "parameters": {"effort": "low"}}
        return result

    original = recorder.io

    def record(record_id, payload):
        if "selection" in payload:
            recorded.append(payload)
            if recording_fails:
                raise OSError("disk full")
        original(record_id, payload)

    monkeypatch.setattr(assessment, "evaluate", evaluate)
    monkeypatch.setattr(recorder, "io", record)
    selection, _ = await assessment.assess(
        {"thread_context_complete": True},
        None,
        person_id="alice",
        logger=logging.getLogger(),
        brain_factory=SimpleNamespace(),
        recorder=recorder,
    )
    assert calls == 1
    assert recorded[0]["selection"]["response_effort"] == "high"
    assert recorded[0]["result"]["configuration"]["parameters"]["effort"] == "low"
    assert selection.route == "agent"
    assert selection.response_effort == (None if recording_fails else "high")
    assert recorder.events[0][1]["response_effort"] == selection.response_effort


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["jev", "llm", "cli"])
async def test_resolved_configuration_is_durable_before_failed_call(
    monkeypatch, engine
):
    from guildbotics.guest.cli_agent import CliAgentBrain
    from guildbotics.intelligences.brains.agno_agent import (
        AgnoAgentDefaultBrain,
        ModelConfig,
    )
    from guildbotics.intelligences.cli_agents import ExecutableInfo

    logger = logging.getLogger()
    if engine == "jev":
        brain = jev.JevBrain("alice", "chat_decision", logger)
        model = "jev-latest"
    elif engine == "llm":
        brain = AgnoAgentDefaultBrain(
            "alice",
            "chat_decision",
            logger,
            model="judge",
            resolved_model=ModelConfig(
                name="models/openai/judge.yml",
                model_class="agno.models.openai.OpenAIChat",
                parameters={
                    "id": "test-model",
                    "temperature": 0.2,
                    "api_key": "private-inline-key",
                },
            ),
        )
        model = "test-model"
    else:
        brain = CliAgentBrain(
            "alice",
            "chat_decision",
            logger,
            cli_agent="judge",
            resolved_cli=ExecutableInfo(
                adapter="codex",
                parameters={
                    "model": "test-cli",
                    "effort": "high",
                    "env": {"KEY": "private-inline-key"},
                },
            ),
        )
        model = "test-cli"
    recorder = Recorder()
    observed = []
    calls = 0

    async def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        observed.append(
            next(p for _, p in recorder.records if p.get("phase") == "resolved")
        )
        raise RuntimeError("private-inline-key")

    monkeypatch.setattr(brain, "run", fail)
    selection, _ = await assessment.assess(
        {"thread_context_complete": True},
        DecisionConfig(),
        person_id="alice",
        logger=logger,
        brain_factory=SimpleNamespace(create_brain=lambda *args, **kwargs: brain),
        recorder=recorder,
    )
    resolved = next(p for _, p in recorder.records if p.get("phase") == "resolved")
    payload = recorder.records[-1][1]
    assert calls == len(observed) == 1
    assert resolved["configuration"] == brain.configuration
    assert resolved["instructions"] == engines.INSTRUCTIONS
    assert "private-inline-key" not in json.dumps(recorder.records)
    assert selection.route == "agent"
    result = payload["result"]
    assert result["error"] == "evaluation_failed"
    assert result["model"] == model
    assert result["configuration"]["provider"]
    if engine != "jev":
        assert result["configuration"]["slot"] == "judge"
        assert result["configuration"]["parameters"] == (
            {"id": "test-model", "temperature": 0.2}
            if engine == "llm"
            else {"model": "test-cli", "effort": "high"}
        )
