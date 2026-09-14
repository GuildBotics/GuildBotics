"""Tests for provider-neutral agent event recording."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from guildbotics.intelligences.agent_environment.contract import (
    AccessContract,
    NetworkPolicy,
)
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


def _record(
    event: AgentEvent,
    recorded: list[dict[str, Any]],
    *,
    contract: AccessContract | None = None,
) -> dict[str, Any]:
    key = ConversationKey("aiko", "codex", "ticket", "issue-1")
    context = AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=Path("."),
        workspace_root=Path("."),
        workspace_data_root=Path("."),
        conversation_key=key,
        contract=contract or AccessContract(),
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


def test_failed_command_records_unmatched_destination_candidates(
    recorded: list[dict[str, Any]],
) -> None:
    _record(
        AgentEvent(
            AgentEventKind.COMMAND,
            "completed",
            message=(
                "connect to blocked.example port 443 failed; "
                "direct connection to 203.0.113.8:8443 failed"
            ),
            command="curl https://blocked.example:443/upload",
        ),
        recorded,
    )

    assert [call["event_type"] for call in recorded] == [
        "agent_runtime.command",
        "agent_environment.network_egress_candidate",
    ]
    assert recorded[1]["payload"] == {
        "name": "network_egress_candidate",
        "message": (
            "A restricted turn emitted destination-shaped evidence that was "
            "not matched to a known allowed destination."
        ),
        "evidence": "command_result",
        "candidates": [
            {
                "destination": "blocked.example",
                "kind": "domain",
                "confidence": "high",
                "port": 443,
            },
            {
                "destination": "203.0.113.8",
                "kind": "ip",
                "confidence": "high",
                "port": 8443,
            },
        ],
    }


def test_allowed_provider_user_and_local_destinations_are_not_candidates(
    recorded: list[dict[str, Any]],
) -> None:
    contract = AccessContract(
        network=NetworkPolicy(
            mode="allowlist",
            allowed_domains=["*.example.com"],
            allow_local_network=True,
        )
    )
    _record(
        AgentEvent(
            AgentEventKind.FAILED,
            "provider",
            message=(
                "https://api.openai.com/v1 failed; "
                "https://cdn.example.com/file failed; 192.168.1.20:443 failed"
            ),
        ),
        recorded,
        contract=contract,
    )

    assert [call["event_type"] for call in recorded] == ["agent_runtime.failed"]


def test_agent_response_is_not_treated_as_network_evidence(
    recorded: list[dict[str, Any]],
) -> None:
    _record(
        AgentEvent(
            AgentEventKind.ASSISTANT,
            "completed",
            message="I tried https://blocked.example but it was denied.",
        ),
        recorded,
    )

    assert [call["event_type"] for call in recorded] == ["agent_runtime.assistant"]


def test_unrestricted_turn_does_not_record_network_candidates(
    recorded: list[dict[str, Any]],
) -> None:
    _record(
        AgentEvent(
            AgentEventKind.FAILED,
            "provider",
            message="https://blocked.example failed",
        ),
        recorded,
        contract=AccessContract(network=NetworkPolicy(mode="unrestricted")),
    )

    assert [call["event_type"] for call in recorded] == ["agent_runtime.failed"]


def test_provider_stderr_candidates_are_bounded(recorded: list[dict[str, Any]]) -> None:
    key = ConversationKey("aiko", "codex", "ticket", "issue-1")
    context = AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=Path("."),
        workspace_root=Path("."),
        workspace_data_root=Path("."),
        conversation_key=key,
    )
    diagnostics.record_network_egress_candidates(
        text=" ".join(f"host{index}.invalid" for index in range(40)),
        context=context,
        adapter_name="codex",
        evidence="provider_stderr",
    )

    payload = recorded[0]["payload"]
    assert payload["evidence"] == "provider_stderr"
    assert len(payload["candidates"]) == diagnostics.MAX_NETWORK_CANDIDATES
