"""Frozen chat adoption and normalization contracts, shared with the guest."""

import itertools
import pytest
from pydantic import ValidationError
from guildbotics.intelligences.decisions.chat_policy import (
    QUESTIONS,
    conjunction,
    disjunction,
    select,
)
from guildbotics.intelligences.decisions.models import Evaluation, Question, Selection
from guildbotics.intelligences.decisions.normalization import normalize


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
    assert set(result.model_dump()) == {
        "route",
        "reason",
        "reaction",
        "response_effort",
    }


@pytest.mark.parametrize(
    "changes,reason",
    [
        (
            {
                "context_sufficient": "unknown",
                "pending_request": "true",
                "work_repo_research": "true",
            },
            "request",
        ),
        ({"context_sufficient": "false", "pending_request": "true"}, "request"),
        ({"pending_request": "unknown", "work_files": "true"}, "work"),
        ({"work_files": "unknown", "work_repo_research": "true"}, "work"),
        (
            {
                "context_sufficient": "unknown",
                "work_files": "unknown",
                "other_role_needed": "true",
            },
            "handoff",
        ),
        (
            {
                "pending_request": "unknown",
                "other_role_needed": "unknown",
                "role_contribution": "true",
            },
            "reply",
        ),
        ({"context_sufficient": "unknown", "pending_request": "unknown"}, "context"),
        ({"pending_request": "unknown", "reaction": "ack"}, "request.unknown"),
        ({"work_files": "unknown"}, "work.unknown"),
        ({"other_role_needed": "true", "handoff_done": "unknown"}, "handoff.unknown"),
        ({"role_contribution": "unknown"}, "reply.unknown"),
    ],
)
def test_definite_obligations_precede_uncertainty(changes, reason):
    result = select(answers(**changes), participation="strict")
    assert (result.route, result.reason) == ("agent", reason)


@pytest.mark.parametrize("failure", ["error", "missing_answer", "incomplete_input"])
def test_invalid_evaluation_precedes_a_definite_request(failure):
    evaluation = answers(pending_request="true")
    if failure == "error":
        evaluation.error = "connection_error"
    if failure == "missing_answer":
        evaluation.answers.pop("reaction")
    selection = select(
        evaluation, participation="strict", input_complete=failure != "incomplete_input"
    )
    assert (selection.route, selection.reason) == ("agent", "invalid")


def test_unrelated_unknowns_do_not_prevent_safe_reaction():
    result = select(
        answers(
            handoff_done="unknown",
            handoff_reopen="unknown",
            role_contribution="unknown",
            ack_only="true",
            repeated_supplement="unknown",
            social_fit="unknown",
            reaction="ack",
        ),
        participation="strict",
    )
    assert (result.route, result.reaction) == ("reaction-only", "ack")
    assert result.response_effort is None


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, "default"),
        ({"work_files": "true"}, "high"),
        ({"work_repo_research": "true"}, "high"),
        ({"work_repo_decision": "true"}, "high"),
        ({"work_files": "unknown"}, None),
        ({"work_files": "unknown", "work_repo_research": "true"}, "high"),
        ({"context_sufficient": "unknown", "work_repo_decision": "true"}, "high"),
    ],
)
def test_response_effort_depends_on_required_work_not_route_reason(changes, expected):
    result = select(answers(pending_request="true", **changes), participation="strict")
    assert result.reason == "request"
    assert result.response_effort == expected


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
        assert (result.route, result.reason) == (
            "agent",
            "request.unknown" if pending_request == 0.5 else "request",
        )
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
        "reason": "invalid",
        "reaction": None,
        "response_effort": None,
    }


def test_a_selection_carries_only_a_semantic_reaction():
    assert Selection(route="agent", reason="r").reaction is None
    assert (
        Selection(route="reaction-only", reason="r", reaction="celebrate").reaction
        == "celebrate"
    )
    with pytest.raises(ValidationError):
        Selection(route="reaction-only", reason="r", reaction="thumbsup")


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
