"""Redacted provider-neutral event recording."""

from __future__ import annotations

import re
from typing import Any

from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentExecutionContext,
    ConversationRecord,
)
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.observability.session_transcripts import should_record_agent_event

MAX_MESSAGE = 8_192
_SENSITIVE_PARTS = ("token", "secret", "password", "credential", "authorization")
_INLINE_SECRET = re.compile(
    r"(?i)(?P<label>(?:--)?(?:access[-_]?token|api[-_]?key|token|password|secret|authorization))"
    r"(?P<separator>\s*(?:=|:)\s*|\s+)"
    r"(?P<value>(?:bearer\s+)?[^\s,;]+)"
)


def record_agent_event(
    event: AgentEvent,
    context: AgentExecutionContext,
    conversation: ConversationRecord,
) -> None:
    if not should_record_agent_event(event.kind.value, event.name):
        return
    payload: dict[str, Any] = {
        "name": event.name,
        "message": _redact_text(event.message or _default_message(event)),
        "command": _redact_text(event.command),
        "path": event.path[:MAX_MESSAGE],
        "approval": event.approval,
        "usage": dict(event.usage),
        "details": _redact(event.details),
    }
    record_correlated_event(
        event_type=f"agent_runtime.{event.kind.value}",
        default_source="agent_runtime",
        person_id=context.person_id,
        attributes={
            "agent.adapter": conversation.key.adapter,
            "agent.run_id": context.run_id,
            "agent.conversation_id": conversation.key.stable_id,
            "agent.conversation_generation": conversation.generation,
            "agent.provider_session_id": event.provider_session_id,
            "agent.provider_turn_id": event.provider_turn_id,
            "agent.context_cursor": context.context_cursor,
            "agent.lease_id": context.lease_id,
        },
        payload=payload,
    )


def _default_message(event: AgentEvent) -> str:
    """Build a human-readable message from the provider-neutral contract fields.

    Every AgentEvent record should carry a message so consumers can render it
    without provider-specific payload knowledge. Only uniform fields are used:
    ``command``, ``approval``, and the cross-adapter usage token keys.
    """
    if event.command:
        return event.command
    if event.approval:
        return event.approval
    parts = [
        f"{label} {event.usage[key]:,}"
        for key, label in (("input_tokens", "input"), ("output_tokens", "output"))
        if key in event.usage
    ]
    return f"{' · '.join(parts)} tokens" if parts else ""


def _redact(value: Any, *, key: str = "") -> Any:
    if any(part in key.lower() for part in _SENSITIVE_PARTS):
        return "***"
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key=key) for item in value[:100]]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    return _redact_text(str(value))


def _redact_text(value: str) -> str:
    bounded = value[:MAX_MESSAGE]
    return _INLINE_SECRET.sub(
        lambda match: f"{match.group('label')}{match.group('separator')}***",
        bounded,
    )
