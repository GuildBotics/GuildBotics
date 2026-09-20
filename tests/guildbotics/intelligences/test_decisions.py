"""Adoption boundaries, independent unknowns, and mandatory replay evidence."""

import itertools
import json
import logging
from types import SimpleNamespace

import pytest

from guildbotics.intelligences.brains import jev
from guildbotics.intelligences.brains.brain import (
    Brain,
    ExecutionMetadata,
    public_parameters,
)
from guildbotics.intelligences.decisions import assessment, engines
from guildbotics.intelligences.decisions.chat_policy import (
    QUESTIONS,
    conjunction,
    disjunction,
    select,
)
from guildbotics.intelligences.decisions.models import (
    DecisionConfig,
    Evaluation,
    Question,
)
from guildbotics.intelligences.decisions.normalization import normalize
from guildbotics.utils.secret_store import KeyringSecretStore


def answers(**overrides):
    values = dict.fromkeys(QUESTIONS, "false")
    values.update(context_sufficient="true", reaction="none")
    values.update(overrides)
    return Evaluation(
        answers=normalize(
            {
                key: {"type": QUESTIONS[key].type, "value": value}
                for key, value in values.items()
            },
            QUESTIONS,
            probabilistic=False,
        )
    )


@pytest.mark.parametrize(
    "changes,participation,route",
    [
        ({"ack_only": "true", "reaction": "ack"}, "strict", "reaction-only"),
        ({}, "strict", "no-op"),
        (
            {"work_files": "true", "pending_request": "true", "reaction": "ack"},
            "strict",
            "agent",
        ),
        ({"context_sufficient": "false"}, "strict", "agent"),
        ({"pending_request": "unknown"}, "strict", "agent"),
        ({"role_contribution": "true"}, "strict", "agent"),
        (
            {"reaction": "support", "role_contribution": "true", "social_fit": "false"},
            "social",
            "reaction-only",
        ),
        ({"handoff_done": "true", "other_role_needed": "true"}, "strict", "no-op"),
        ({"handoff_done": "false", "other_role_needed": "true"}, "strict", "agent"),
        (
            {
                "handoff_done": "true",
                "handoff_reopen": "true",
                "other_role_needed": "true",
            },
            "strict",
            "agent",
        ),
        (
            {
                "handoff_done": "unknown",
                "handoff_reopen": "unknown",
                "social_fit": "unknown",
            },
            "strict",
            "no-op",
        ),
        ({"reaction": "unknown"}, "strict", "agent"),
        ({"ack_only": "true", "role_contribution": "unknown"}, "strict", "no-op"),
        ({"role_contribution": "unknown"}, "strict", "agent"),
        ({"work_repo_decision": "unknown"}, "strict", "agent"),
        ({"work_files": "true", "work_repo_decision": "unknown"}, "strict", "agent"),
    ],
)
def test_adoption(changes, participation, route):
    result = select(answers(**changes), participation=participation)
    assert result.route == route
    assert set(result.model_dump()) == {"route", "reason", "reaction"}


@pytest.mark.parametrize(
    "values", list(itertools.product(("true", "false", "unknown"), repeat=3))
)
def test_three_valued_algebra(values):
    possibilities = list(
        itertools.product(
            *[(True, False) if v == "unknown" else (v == "true",) for v in values]
        )
    )
    for combine, operation in [(conjunction, all), (disjunction, any)]:
        outcomes = {operation(p) for p in possibilities}
        expected = (
            "unknown" if len(outcomes) > 1 else "true" if True in outcomes else "false"
        )
        assert combine(*values) == expected


@pytest.mark.parametrize(
    "prob,value",
    [
        (0.4, "false"),
        (0.400001, "unknown"),
        (0.5, "unknown"),
        (0.599999, "unknown"),
        (0.6, "true"),
    ],
)
def test_jev_noul_thresholds(prob, value):
    raw = {"context_sufficient": {"type": "noul", "noul": prob}}
    answer = normalize(
        raw, {"context_sufficient": QUESTIONS["context_sufficient"]}, probabilistic=True
    )["context_sufficient"]
    assert answer.value == value
    assert answer.raw == raw["context_sufficient"]
    assert answer.confidence is None


@pytest.mark.parametrize("p", [0.2, 0.34, 0.6, 0.9])
@pytest.mark.parametrize("choice", ["ack", "none"])
def test_choice_adopts_top_option_without_a_confidence_threshold(p, choice):
    item = {
        "type": "choice",
        "choice": choice,
        "confidence": 0.0,
        "probabilities": {
            key: p if key == choice else (1 - p) / 4
            for key in QUESTIONS["reaction"].criteria
        },
    }
    answer = normalize(
        {"reaction": item}, {"reaction": QUESTIONS["reaction"]}, probabilistic=True
    )["reaction"]
    assert answer.value == choice
    assert answer.probabilities == item["probabilities"]
    assert answer.confidence == 0.0


@pytest.mark.parametrize("choice", ["ack", "none"])
@pytest.mark.parametrize(
    "pending_request,needs_agent", [(0.1, False), (0.5, True), (0.9, True)]
)
def test_top_reaction_only_applies_after_noul_obligations_are_excluded(
    choice, pending_request, needs_agent
):
    raw = {
        key: {"type": "noul", "noul": 1.0 if key == "context_sufficient" else 0.0}
        for key, question in QUESTIONS.items()
        if question.type == "noul"
    }
    raw["pending_request"]["noul"] = pending_request
    raw["reaction"] = {
        "type": "choice",
        "choice": choice,
        "confidence": 0.1,
        "probabilities": {
            key: 0.34 if key == choice else 0.165
            for key in QUESTIONS["reaction"].criteria
        },
    }
    result = select(
        Evaluation(answers=normalize(raw, QUESTIONS, probabilistic=True)),
        participation="strict",
    )
    if needs_agent:
        assert (result.route, result.reason) == ("agent", "2.request")
    elif choice == "none":
        assert result.route == "no-op"
    else:
        assert (result.route, result.reaction) == ("reaction-only", choice)


def test_choice_rejects_an_option_below_the_maximum_probability():
    with pytest.raises(ValueError, match="contradictory_choice"):
        normalize(
            {
                "q": {
                    "type": "choice",
                    "choice": "ack",
                    "confidence": 0.1,
                    "probabilities": {"ack": 0.4, "none": 0.6},
                }
            },
            {
                "q": Question(
                    type="choice",
                    instructions="reaction",
                    criteria={"ack": "Receipt", "none": "No reaction"},
                )
            },
            probabilistic=True,
        )


@pytest.mark.parametrize(
    "value", [True, "0.9", None, -0.1, 1.1, float("nan"), float("inf")]
)
def test_invalid_probability_is_not_adopted(value):
    with pytest.raises(ValueError):
        normalize(
            {"q": {"type": "noul", "noul": value}},
            {"q": Question(type="noul", instructions="q")},
            probabilistic=True,
        )


def test_self_reported_confidence_does_not_change_assertion():
    answer = normalize(
        {"q": {"type": "noul", "value": "true", "confidence": 0.01}},
        {"q": Question(type="noul", instructions="q")},
        probabilistic=False,
    )["q"]
    assert answer.value == "true"
    assert answer.provenance == "structured_assertion"


@pytest.mark.parametrize("failure", ["error", "malformed", "incomplete"])
def test_failed_judgment_delegates_without_execution_settings(failure):
    evaluation = (
        Evaluation(error="failed")
        if failure == "error"
        else Evaluation()
        if failure == "malformed"
        else answers(work_files="true")
    )
    result = select(
        evaluation, participation="strict", input_complete=failure != "incomplete"
    )
    assert result.model_dump() == {
        "route": "agent",
        "reason": "1.invalid",
        "reaction": "",
    }


@pytest.mark.asyncio
async def test_snapshot_is_full_and_replayable_even_without_transcripts(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    state = {
        "thread_context_complete": True,
        "unprocessed_messages": [{"content": "ありがとう" * 600}],
        "run_id": "run",
    }

    async def evaluate(*args, **kwargs):
        return answers(reaction="ack")

    redaction_loads = 0

    def load_redaction_values():
        nonlocal redaction_loads
        redaction_loads += 1
        return ()

    monkeypatch.setattr(assessment, "evaluate", evaluate)
    monkeypatch.setattr(
        assessment, "load_required_io_redaction_values", load_redaction_values
    )
    result, record_id = await assessment.assess(
        state,
        DecisionConfig(),
        config_dir=tmp_path,
        person_id="alice",
        logger=logging.getLogger(),
    )
    paths = list(tmp_path.rglob(f"{record_id}.json"))
    assert len(paths) == 1
    payload = json.loads(paths[0].read_text(encoding="utf-8"))["payload"]
    assert payload["input"]["state"] == state
    assert set(payload["input"]["questions"]) == set(QUESTIONS)
    assert result.route == "reaction-only"
    assert redaction_loads == 1
    assert not list(tmp_path.glob(".guildbotics/state/**/required-io/*"))


@pytest.mark.asyncio
async def test_record_failure_cannot_skip_agent(tmp_path, monkeypatch):
    async def evaluate(*args, **kwargs):
        return answers()

    monkeypatch.setattr(assessment, "evaluate", evaluate)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(assessment, "record_required_io", fail)
    result, _ = await assessment.assess(
        {"thread_context_complete": True},
        DecisionConfig(),
        config_dir=tmp_path,
        person_id="alice",
        logger=logging.getLogger(),
    )
    assert result.route == "agent"


def test_generic_choice_is_not_restricted_to_chat_reactions():
    result = normalize(
        {
            "q": {
                "type": "choice",
                "choice": "check",
                "confidence": 0.95,
                "probabilities": {"check": 0.98, "other": 0.02},
            }
        },
        {
            "q": Question(
                type="choice",
                instructions="Which?",
                criteria={"check": "Check", "other": "Other"},
            )
        },
        probabilistic=True,
    )
    assert result["q"].value == "check"


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
    async def request(_root, _method, _path, payload):
        assert payload["model"] == "jev-latest"
        return {
            "model": "jev-future-test-version",
            "answers": {},
            "usage": {"input_tokens": 12},
        }

    monkeypatch.setattr(jev, "request", request)
    brain = jev.JevBrain("alice", "chat_decision", logging.getLogger())
    await brain.run(json.dumps({"state": "test", "questions": {}}))
    assert brain.execution.model == "jev-future-test-version"
    assert brain.execution.usage == {"input_tokens": 12}


@pytest.mark.parametrize("model", ["jev-1.13.0", "jev-preview"])
def test_jev_rejects_other_models(model):
    with pytest.raises(ValueError, match="Unsupported Jev model"):
        jev.JevBrain("alice", "chat_decision", logging.getLogger(), model=model)


@pytest.mark.asyncio
async def test_jev_one_request_and_failure_is_sanitized(tmp_path, monkeypatch):
    calls = []

    async def request(*args):
        calls.append(args)
        raise RuntimeError("private-test-key")

    monkeypatch.setattr(jev, "request", request)
    brain = jev.JevBrain("alice", "chat_decision", logging.getLogger())
    factory = SimpleNamespace(create_brain=lambda *args, **kwargs: brain)
    result = await engines.evaluate(
        DecisionConfig(),
        {"text": "依頼"},
        QUESTIONS,
        config_dir=tmp_path,
        person_id="alice",
        logger=logging.getLogger(),
        brain_factory=factory,
    )
    assert len(calls) == 1
    assert set(calls[0][-1]["questions"]) == set(QUESTIONS)
    assert "private-test-key" not in result.model_dump_json()
    assert result.error


@pytest.mark.asyncio
async def test_common_factory_run_and_execution_metadata(tmp_path):
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
        config_dir=tmp_path,
        person_id="alice",
        logger=logging.getLogger(),
        brain_factory=SimpleNamespace(create_brain=create),
    )
    assert not result.error
    assert len(calls) == 1
    request, kwargs = calls[0]
    assert request["state"] == state and set(request["questions"]) == set(QUESTIONS)
    assert kwargs["input_only"] is True
    assert not {"effort", "model", "session_state"} & kwargs.keys()
    assert result.model == "resolved-model" and result.usage == {"input_tokens": 12}
    assert result.retries is None


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["jev", "llm", "cli"])
async def test_resolved_configuration_is_durable_before_failed_call(
    tmp_path, monkeypatch, engine
):
    from guildbotics.intelligences.brains import agno_agent, cli_agent

    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    logger = logging.getLogger()
    if engine == "jev":
        brain = jev.JevBrain("alice", "chat_decision", logger)
        model = "jev-latest"
    elif engine == "llm":
        monkeypatch.setattr(
            agno_agent,
            "get_model_mapping",
            lambda _: {
                "judge": agno_agent.ModelConfig(
                    name="models/openai/judge.yml",
                    model_class="agno.models.openai.OpenAIChat",
                    parameters={
                        "id": "test-model",
                        "temperature": 0.2,
                        "api_key": "private-inline-key",
                    },
                )
            },
        )
        brain = agno_agent.AgnoAgentDefaultBrain(
            "alice", "chat_decision", logger, model="judge"
        )
        model = "test-model"
    else:
        monkeypatch.setattr(
            cli_agent,
            "get_cli_agent_mapping",
            lambda _: {
                "judge": cli_agent.ExecutableInfo(
                    adapter="codex",
                    parameters={
                        "model": "test-cli",
                        "effort": "high",
                        "env": {"KEY": "private-inline-key"},
                    },
                )
            },
        )
        brain = cli_agent.CliAgentBrain(
            "alice", "chat_decision", logger, cli_agent="judge"
        )
        model = "test-cli"

    observed = []

    async def fail(*args, **kwargs):
        records = [
            json.loads(p.read_text())["payload"]
            for p in tmp_path.rglob("*.json")
            if "required-io" in p.parts
        ]
        resolved = next(p for p in records if p.get("phase") == "resolved")
        observed.append(resolved)
        raise RuntimeError("private-inline-key")

    monkeypatch.setattr(brain, "run", fail)
    selection, record_id = await assessment.assess(
        {"thread_context_complete": True},
        DecisionConfig(),
        config_dir=tmp_path,
        person_id="alice",
        logger=logger,
        brain_factory=SimpleNamespace(create_brain=lambda *args, **kwargs: brain),
    )
    path = next(tmp_path.rglob(f"{record_id}.json"))
    payload = json.loads(path.read_text())["payload"]
    assert len(observed) == 1
    assert observed[0]["configuration"] == brain.configuration
    assert observed[0]["instructions"] == engines.INSTRUCTIONS
    assert "private-inline-key" not in path.read_text()
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


@pytest.mark.asyncio
async def test_failed_configuration_record_does_not_call_model(tmp_path, monkeypatch):
    brain = jev.JevBrain("alice", "chat_decision", logging.getLogger())
    called = False

    async def run(*args, **kwargs):
        nonlocal called
        called = True

    def record(_id, payload, **_kwargs):
        if payload.get("phase") == "resolved":
            raise OSError("disk full")

    monkeypatch.setattr(brain, "run", run)
    monkeypatch.setattr(assessment, "record_required_io", record)
    selection, _ = await assessment.assess(
        {"thread_context_complete": True},
        DecisionConfig(),
        config_dir=tmp_path,
        person_id="alice",
        logger=logging.getLogger(),
        brain_factory=SimpleNamespace(create_brain=lambda *args, **kwargs: brain),
    )
    assert not called
    assert selection.route == "agent"


@pytest.mark.asyncio
async def test_jev_credentials_are_fresh_and_never_fall_back_to_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(jev.JEV_KEY, "stale-secret")
    assert not jev.credential(tmp_path)
    store = KeyringSecretStore(tmp_path)
    store.set(jev.JEV_KEY, "current-secret")
    assert jev.credential(tmp_path) == "current-secret"
    store.delete(jev.JEV_KEY)
    with pytest.raises(ValueError, match="credentials_missing"):
        await jev.request(tmp_path, "POST", "/systemone", {})
