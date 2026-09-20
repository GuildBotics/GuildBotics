"""Adoption boundaries, independent unknowns, and mandatory replay evidence."""

import itertools
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from guildbotics.intelligences.decisions import assessment, engines, settings
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
    values = {key: "false" for key in QUESTIONS}
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
    "changes,participation,route,effort",
    [
        ({"ack_only": "true", "reaction": "ack"}, "strict", "reaction-only", ""),
        ({}, "strict", "no-op", ""),
        (
            {"pending_request": "true", "effort_files": "true", "reaction": "ack"},
            "strict",
            "agent",
            "high",
        ),
        ({"context_sufficient": "false"}, "strict", "agent", "default"),
        ({"pending_request": "unknown"}, "strict", "agent", "default"),
        ({"role_contribution": "true"}, "strict", "agent", "default"),
        (
            {"role_contribution": "true", "social_fit": "false", "reaction": "support"},
            "social",
            "reaction-only",
            "",
        ),
        ({"other_role_needed": "true", "handoff_done": "true"}, "strict", "no-op", ""),
        (
            {"other_role_needed": "true", "handoff_done": "false"},
            "strict",
            "agent",
            "default",
        ),
        (
            {
                "other_role_needed": "true",
                "handoff_done": "true",
                "handoff_reopen": "true",
            },
            "strict",
            "agent",
            "default",
        ),
        (
            {
                "handoff_done": "unknown",
                "handoff_reopen": "unknown",
                "social_fit": "unknown",
            },
            "strict",
            "no-op",
            "",
        ),
        ({"reaction": "unknown"}, "strict", "agent", "default"),
        ({"role_contribution": "unknown", "ack_only": "true"}, "strict", "no-op", ""),
        ({"role_contribution": "unknown"}, "strict", "agent", "default"),
        ({"effort_repo_decision": "unknown"}, "strict", "agent", "high"),
    ],
)
def test_adoption(changes, participation, route, effort):
    result = select(answers(**changes), participation=participation)
    assert (result.route, result.effort) == (route, effort)


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
    [(0.1, "false"), (0.100001, "unknown"), (0.899999, "unknown"), (0.9, "true")],
)
def test_jev_noul_thresholds(prob, value):
    raw = {"context_sufficient": {"type": "noul", "noul": prob}}
    answer = normalize(
        raw, {"context_sufficient": QUESTIONS["context_sufficient"]}, probabilistic=True
    )["context_sufficient"]
    assert answer.value == value
    assert answer.raw == raw["context_sufficient"]
    assert answer.confidence is None


@pytest.mark.parametrize("p,value", [(0.899999, "unknown"), (0.9, "ack")])
def test_choice_uses_probability_not_confidence(p, value):
    item = {
        "type": "choice",
        "choice": "ack",
        "confidence": 0.2,
        "probabilities": {
            "ack": p,
            "agree": 1 - p,
            "celebrate": 0,
            "support": 0,
            "none": 0,
        },
    }
    answer = normalize(
        {"reaction": item}, {"reaction": QUESTIONS["reaction"]}, probabilistic=True
    )["reaction"]
    assert answer.value == value
    assert answer.confidence == 0.2


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


def test_failure_and_existing_high():
    assert (
        select(Evaluation(error="failed"), participation="strict").effort_reason
        == "effort.failure"
    )
    result = select(
        answers(pending_request="true"), participation="strict", previous_effort="high"
    )
    assert result.effort == "high"
    assert result.effort_reason == "effort.preserved"
    assert (
        select(answers(), participation="strict", previous_effort="high").effort == ""
    )


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

    monkeypatch.setattr(assessment, "evaluate", evaluate)
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
    assert result.effort == "high"


def test_credentials_are_local_and_deletion_is_observed(tmp_path, monkeypatch):
    monkeypatch.delenv(settings.JEV_KEY, raising=False)
    config = DecisionConfig(engine="jev", model="jev-1.13.0")
    assert settings.availability(config, tmp_path).state == "missing"
    store = KeyringSecretStore(tmp_path)
    store.set(settings.JEV_KEY, "test-credential")
    ready = settings.availability(config, tmp_path)
    assert ready.available and ready.state == "unverified"
    store.delete(settings.JEV_KEY)
    monkeypatch.setenv(settings.JEV_KEY, "stale-key-loaded-at-startup")
    assert not settings.availability(config, tmp_path).available


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


def test_failed_model_check_does_not_poison_another_model(tmp_path):
    KeyringSecretStore(tmp_path).set(settings.JEV_KEY, "test-credential")
    failed = DecisionConfig(engine="jev", model="jev-preview")
    settings.record_connection(failed, tmp_path, "invalid")
    assert not settings.availability(failed, tmp_path).available
    assert settings.availability(
        DecisionConfig(engine="jev", model="jev-latest"), tmp_path
    ).available


@pytest.mark.asyncio
async def test_jev_one_request_and_failure_is_sanitized(tmp_path, monkeypatch):
    KeyringSecretStore(tmp_path).set(settings.JEV_KEY, "private-test-key")
    calls = []

    async def request(*args):
        calls.append(args)
        raise RuntimeError("private-test-key")

    monkeypatch.setattr(engines, "jev_request", request)
    result = await engines.evaluate(
        DecisionConfig(engine="jev", model="jev-latest"),
        {"text": "依頼"},
        QUESTIONS,
        config_dir=tmp_path,
        person_id="alice",
        logger=logging.getLogger(),
    )
    assert len(calls) == 1
    assert set(calls[0][-1]["questions"]) == set(QUESTIONS)
    assert "private-test-key" not in result.model_dump_json()
    assert result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["agno", "cli"])
async def test_structured_engines_use_same_questions_without_external_tools(
    tmp_path, monkeypatch, engine
):
    state = {"text": "この対応は取り消します"}
    raw = {
        "answers": {
            key: {"type": q.type, "value": "none" if q.type == "choice" else "false"}
            for key, q in QUESTIONS.items()
        }
    }
    monkeypatch.setattr(
        engines, "availability", lambda *args: SimpleNamespace(available=True)
    )
    calls = []
    environment = None
    if engine == "agno":

        class Brain:
            def __init__(self, *args, **kwargs):
                assert kwargs["model_config"].parameters["id"] == "test-model"

            async def run_with_execution_details(self, prompt, **kwargs):
                assert kwargs == {"tools": [], "tool_call_limit": 0}
                calls.append(json.loads(prompt))
                return SimpleNamespace(
                    content=json.dumps(raw),
                    model="actual-model",
                    usage={"input_tokens": 12},
                )

        monkeypatch.setattr(engines, "AgnoAgentDefaultBrain", Brain)
        provider = "openai"
    else:
        stdin = SimpleNamespace(
            write=lambda data: calls.append(json.loads(data)),
            drain=AsyncMock(),
            close=lambda: None,
        )
        process = SimpleNamespace(
            stdin=stdin,
            communicate=AsyncMock(
                return_value=(
                    json.dumps(
                        {
                            "result": json.dumps(raw),
                            "modelUsage": {"actual-model": {}},
                            "usage": {"input_tokens": 12},
                        }
                    ).encode(),
                    b"",
                )
            ),
            wait=AsyncMock(return_value=0),
        )
        environment = SimpleNamespace(
            run=AsyncMock(return_value=process), close=AsyncMock()
        )
        monkeypatch.setattr(
            engines, "start_probe_environment", AsyncMock(return_value=environment)
        )
        provider = "claude"
    result = await engines.evaluate(
        DecisionConfig(engine=engine, provider=provider, model="test-model"),
        state,
        QUESTIONS,
        config_dir=tmp_path,
        person_id="alice",
        logger=logging.getLogger(),
    )
    assert not result.error
    assert len(calls) == 1 and calls[0]["state"] == state
    assert set(calls[0]["questions"]) == set(QUESTIONS)
    assert result.model == "actual-model" and result.usage == {"input_tokens": 12}
    assert result.retries is None
    if environment:
        args = environment.run.call_args.args
        assert args[args.index("--tools") + 1] == ""
        assert "--no-session-persistence" in args and "--strict-mcp-config" in args
        environment.close.assert_awaited_once()
