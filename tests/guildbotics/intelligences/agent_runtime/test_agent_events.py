"""Tests for provider-neutral agent event recording."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from guildbotics.intelligences.agent_runtime import diagnostics
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentEventKind,
    AgentExecutionContext,
    ConversationKey,
    ConversationRecord,
)


@pytest.fixture(name="recorded")
def recorded_fixture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(diagnostics, "record_correlated_event", capture)
    return calls


def _record(event: AgentEvent, recorded: list[dict[str, Any]]) -> dict[str, Any]:
    key = ConversationKey("aiko", "codex", "ticket", "issue-1")
    context = AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=Path("."),
        workspace_data_root=Path("."),
        conversation_key=key,
    )
    diagnostics.record_agent_event(event, context, ConversationRecord(key=key))
    return recorded[-1]["payload"]


def test_explicit_message_is_kept(recorded: list[dict[str, Any]]) -> None:
    payload = _record(
        AgentEvent(AgentEventKind.ASSISTANT, "completed", message="all done"),
        recorded,
    )
    assert payload["message"] == "all done"


def test_command_events_use_the_command_as_message(
    recorded: list[dict[str, Any]],
) -> None:
    payload = _record(
        AgentEvent(AgentEventKind.COMMAND, "started", command="uv run pytest"),
        recorded,
    )
    assert payload["message"] == "uv run pytest"


def test_approval_events_use_the_decision_as_message(
    recorded: list[dict[str, Any]],
) -> None:
    payload = _record(
        AgentEvent(AgentEventKind.APPROVAL, "decision", approval="approved"),
        recorded,
    )
    assert payload["message"] == "approved"


def test_usage_events_summarize_cross_adapter_token_keys(
    recorded: list[dict[str, Any]],
) -> None:
    payload = _record(
        AgentEvent(
            AgentEventKind.USAGE,
            "updated",
            usage={
                "input_tokens": 12345,
                "output_tokens": 678,
                "cached_input_tokens": 999,
            },
        ),
        recorded,
    )
    assert payload["message"] == "input 12,345 · output 678 tokens"


def test_events_without_content_keep_an_empty_message(
    recorded: list[dict[str, Any]],
) -> None:
    payload = _record(AgentEvent(AgentEventKind.TURN, "started"), recorded)
    assert payload["message"] == ""


def test_synthesized_messages_are_redacted(recorded: list[dict[str, Any]]) -> None:
    payload = _record(
        AgentEvent(
            AgentEventKind.COMMAND,
            "started",
            command="deploy --api-key=super-secret",
        ),
        recorded,
    )
    assert "super-secret" not in payload["message"]
    assert payload["message"].startswith("deploy --api-key=")
