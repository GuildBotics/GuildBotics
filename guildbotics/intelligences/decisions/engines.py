"""Evaluate through the same Brain factory and run interface as commands."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict
from logging import Logger
from typing import Any

import httpx

from guildbotics.editions import get_edition
from guildbotics.intelligences.brains.cli_agent import CliAgentBrain
from guildbotics.intelligences.decisions.models import (
    DecisionConfig,
    Evaluation,
    Question,
)
from guildbotics.intelligences.decisions.normalization import normalize
from guildbotics.runtime.brain_factory import BrainFactory

INSTRUCTIONS = (
    "Evaluate every question independently using only the supplied state. "
    'Return JSON {"answers": {question_id: {"type": "noul" or "choice", "value": answer}}}. '
    'For noul use "true", "false", "unknown". For choice use a criterion key or "unknown". '
    "Do not infer unknown as false or none. Do not use tools or external context. No prose or Markdown."
)


async def evaluate(
    config: DecisionConfig,
    state: dict[str, Any],
    questions: dict[str, Question],
    *,
    person_id: str,
    logger: Logger,
    brain_factory: BrainFactory | None = None,
    on_resolved: Callable[[dict[str, Any]], None] | None = None,
) -> Evaluation:
    """One invocation; any execution or answer failure delegates to the agent."""
    raw: Any = None
    brain = None
    result = Evaluation()
    try:
        factory = brain_factory or get_edition().get_context().brain_factory
        brain = factory.create_brain(
            person_id,
            "chat_decision",
            "",
            logger,
            config={"brain": config.brain, "body": INSTRUCTIONS},
        )
        result.configuration = brain.configuration
        result.model = str(result.configuration.get("model", ""))
        if on_resolved is not None:
            try:
                on_resolved(result.configuration)
            except Exception:
                result.error = "recording_failed"
                return result
        if isinstance(brain, CliAgentBrain):
            raise ValueError("AI CLI is not supported for chat_decision")
        request = {
            "state": state,
            "questions": {
                key: q.model_dump(exclude_defaults=True) for key, q in questions.items()
            },
        }
        async with asyncio.timeout(120):
            raw = await brain.run(
                json.dumps(request, ensure_ascii=False, sort_keys=True)
            )
        if isinstance(raw, str):
            raw = json.loads(raw)
        result.raw = raw
        result.answers = normalize(
            raw["answers"], questions, probabilistic=brain.probabilistic_answers
        )
    except Exception as exc:
        # Provider exception messages may contain credentials.
        result.raw = raw
        result.error = error_code(exc)
    if brain is not None:
        result = result.model_copy(
            update={
                **asdict(brain.execution),
                "model": brain.execution.model or result.model,
            },
        )
    return result


def error_code(exc: Exception) -> str:
    if getattr(exc, "status_code", None) in {401, 403}:
        return "authentication_error"
    if isinstance(exc, httpx.HTTPStatusError):
        return (
            "authentication_error"
            if exc.response.status_code in {401, 403}
            else "connection_error"
        )
    if isinstance(exc, (httpx.RequestError, TimeoutError)):
        return "connection_error"
    return (
        "invalid_response"
        if isinstance(exc, (ValueError, KeyError, TypeError))
        else "evaluation_failed"
    )
