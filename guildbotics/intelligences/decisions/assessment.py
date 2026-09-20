"""Evaluate, adopt and durably record one immutable input snapshot."""

import hashlib
import json
import time
from logging import Logger
from pathlib import Path
from typing import Any
from uuid import uuid4

from guildbotics.intelligences.decisions.chat_policy import (
    ADOPTION_VERSION,
    QUESTION_VERSION,
    QUESTIONS,
    RULE_VERSION,
    select,
)
from guildbotics.intelligences.decisions.engines import INSTRUCTIONS, evaluate
from guildbotics.intelligences.decisions.models import (
    DecisionConfig,
    Evaluation,
    Question,
    Selection,
)
from guildbotics.observability.diagnostics_events import (
    load_required_io_redaction_values,
    record_correlated_event,
    record_required_io,
)
from guildbotics.runtime.brain_factory import BrainFactory


async def assess(
    state: dict[str, Any],
    config: DecisionConfig | None,
    *,
    config_dir: Path,
    person_id: str,
    logger: Logger,
    questions: dict[str, Question] | None = None,
    brain_factory: BrainFactory | None = None,
) -> tuple[Selection, str]:
    """Return a fast path only after both the input and decision were stored."""
    questions = QUESTIONS if questions is None else questions
    evaluation_id = uuid4().hex
    started = time.monotonic()
    config = config or DecisionConfig()
    request = {
        "state": state,
        "questions": {key: q.model_dump() for key, q in questions.items()},
    }
    recording_failed = False
    redaction_values: tuple[str, ...] | None = None

    def record_configuration(configuration: dict[str, Any]) -> None:
        if redaction_values is None:
            raise RuntimeError("Required IO redaction values are unavailable")
        record_required_io(
            evaluation_id + "1",
            {
                "evaluation_id": evaluation_id,
                "phase": "resolved",
                "config": config.model_dump(),
                "configuration": configuration,
                "instructions": INSTRUCTIONS,
            },
            redaction_values=redaction_values,
        )

    try:
        redaction_values = load_required_io_redaction_values()
        record_required_io(
            evaluation_id + "0",
            {
                "evaluation_id": evaluation_id,
                "phase": "started",
                "input": request,
                "config": config.model_dump(),
            },
            redaction_values=redaction_values,
        )
    except Exception:
        recording_failed = True
    try:
        error = (
            "recording_failed"
            if recording_failed
            else "incomplete_input"
            if state.get("thread_context_complete") is not True
            else ""
        )
        result = (
            Evaluation(error=error)
            if error
            else await evaluate(
                config,
                state,
                questions,
                config_dir=config_dir,
                person_id=person_id,
                logger=logger,
                brain_factory=brain_factory,
                on_resolved=record_configuration,
            )
        )
    except Exception:
        result = Evaluation(error="evaluation_failed")
    selection = select(
        Evaluation(error="recording_failed") if recording_failed else result,
        participation=state.get("chat_participation", "strict"),
        input_complete=state.get("thread_context_complete") is True,
    )
    payload = {
        "evaluation_id": evaluation_id,
        "person_id": person_id,
        "input": request,
        "input_hash": hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "question_version": QUESTION_VERSION,
        "rule_version": RULE_VERSION,
        "adoption_version": ADOPTION_VERSION,
        "config": config.model_dump(),
        "result": result.model_dump(),
        "selection": selection.model_dump(),
        "duration_ms": (time.monotonic() - started) * 1000,
    }
    try:
        if redaction_values is None:
            raise RuntimeError("Required IO redaction values are unavailable")
        record_required_io(evaluation_id, payload, redaction_values=redaction_values)
    except Exception:
        recording_failed = True
        selection = Selection(route="agent", reason="invalid")
    record_correlated_event(
        event_type="decision.evaluated",
        person_id=person_id,
        payload={
            "evaluation_id": evaluation_id,
            "brain": config.brain,
            "model": result.model,
            "route": selection.route,
            "reason": selection.reason,
            "error": "recording_failed" if recording_failed else result.error,
            "duration_ms": payload["duration_ms"],
        },
    )
    return selection, evaluation_id
