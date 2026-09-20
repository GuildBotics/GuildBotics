"""Validate every answer before allowing application policy to adopt any."""

import math
from typing import Any

from guildbotics.intelligences.decisions.models import Answer, Question

NOUL_YES_THRESHOLD = 0.6
NOUL_NO_THRESHOLD = 0.4
CHOICE_THRESHOLD = 0.9


def probability(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("invalid_probability")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("invalid_probability")
    return float(value)


def normalize(
    raw: Any, questions: dict[str, Question], *, probabilistic: bool
) -> dict[str, Answer]:
    if not isinstance(raw, dict) or set(raw) != set(questions):
        raise ValueError("missing_or_extra_answers")
    answers = {}
    for key, question in questions.items():
        item = raw[key]
        if not isinstance(item, dict) or item.get("type") != question.type:
            raise ValueError("invalid_answer_type")
        probabilities = None
        confidence = None
        if probabilistic:
            if question.type == "noul":
                p = probability(item.get("noul"))
                probabilities = {"true": p, "false": 1 - p}
                value: Any = (
                    "true"
                    if p >= NOUL_YES_THRESHOLD
                    else "false"
                    if p <= NOUL_NO_THRESHOLD
                    else "unknown"
                )
            else:
                distribution = item.get("probabilities")
                if not isinstance(distribution, dict) or set(distribution) != set(
                    question.criteria
                ):
                    raise ValueError("invalid_distribution")
                probabilities = {
                    name: probability(p) for name, p in distribution.items()
                }
                choice = item.get("choice")
                if choice not in probabilities or not math.isclose(
                    sum(probabilities.values()), 1, abs_tol=0.001
                ):
                    raise ValueError("invalid_choice")
                if probabilities[choice] != max(probabilities.values()):
                    raise ValueError("contradictory_choice")
                confidence = probability(item.get("confidence"))
                value = (
                    choice if probabilities[choice] >= CHOICE_THRESHOLD else "unknown"
                )
        else:
            value = item.get("value")
            permitted = (
                {"true", "false", "unknown"}
                if question.type == "noul"
                else {*question.criteria, "unknown"}
            )
            if not isinstance(value, str) or value not in permitted:
                raise ValueError("invalid_assertion")
            if item.get("confidence") is not None:
                confidence = probability(item["confidence"])
        answers[key] = Answer(
            value=value,
            raw=item,
            probabilities=probabilities,
            confidence=confidence,
            provenance="jev_distribution" if probabilistic else "structured_assertion",
        )
    return answers
