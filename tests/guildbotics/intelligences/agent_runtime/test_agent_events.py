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
    # The turns recorded here run in a command whose contract is the default.
    monkeypatch.setattr(diagnostics, "current_command_contract", AccessContract)
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
    )
    with pytest.MonkeyPatch.context() as patch:
        if contract is not None:
            patch.setattr(diagnostics, "current_command_contract", lambda: contract)
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
            details={"status": "failed"},
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
                "port": 443,
            },
            {
                "destination": "203.0.113.8",
                "kind": "ip",
                "port": 8443,
            },
        ],
    }


def test_allowed_user_and_local_destinations_are_not_candidates(
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
            message="https://cdn.example.com/file failed; 192.168.1.20:443 failed",
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


def test_successful_command_output_is_not_treated_as_network_evidence(
    recorded: list[dict[str, Any]],
) -> None:
    _record(
        AgentEvent(
            AgentEventKind.COMMAND,
            "completed",
            message=(
                "README.md setup.py package.json src/app.tsx\n"
                "18:32:08 users.noreply.github.com"
            ),
            command="ls",
            details={"status": "completed"},
        ),
        recorded,
    )

    assert [call["event_type"] for call in recorded] == ["agent_runtime.command"]


def test_bare_hosts_ips_and_timestamps_are_not_destination_candidates(
    recorded: list[dict[str, Any]],
) -> None:
    _record(
        AgentEvent(
            AgentEventKind.FAILED,
            "provider",
            message=('blocked.example 203.0.113.8 18:32:08 No module named "foo.bar"'),
        ),
        recorded,
    )

    assert [call["event_type"] for call in recorded] == ["agent_runtime.failed"]


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


def test_no_candidates_are_recorded_outside_a_command(
    recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a running command there is no contract to hold evidence to."""
    monkeypatch.setattr(diagnostics, "current_command_contract", lambda: None)
    _record(
        AgentEvent(
            AgentEventKind.FAILED,
            "provider",
            message="https://blocked.example failed",
        ),
        recorded,
    )

    assert [call["event_type"] for call in recorded] == ["agent_runtime.failed"]


def test_failed_event_candidates_are_bounded(recorded: list[dict[str, Any]]) -> None:
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
        text=" ".join(f"https://host{index}.invalid/path" for index in range(40)),
        context=context,
        adapter_name="codex",
        evidence="failed_event",
    )

    payload = recorded[0]["payload"]
    assert payload["evidence"] == "failed_event"
    assert len(payload["candidates"]) == diagnostics.MAX_NETWORK_CANDIDATES


def test_a_provider_destination_is_a_candidate_unless_its_turn_opens_it(
    recorded: list[dict[str, Any]],
) -> None:
    """A brokered turn reaches its API through the gateway, not by name: a
    destination there is as unmatched as any other, unlike what the turn
    opens directly."""
    key = ConversationKey("aiko", "antigravity", "ticket", "issue-1")
    context = AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=Path("."),
        workspace_root=Path("."),
        workspace_data_root=Path("."),
        conversation_key=key,
    )
    diagnostics.record_network_egress_candidates(
        text=(
            "https://accounts.google.com/o/oauth2 failed; "
            "https://lh3.googleusercontent.com/a/picture failed"
        ),
        context=context,
        adapter_name="antigravity",
        evidence="failed_event",
    )

    (call,) = recorded
    hosts = [c["destination"] for c in call["payload"]["candidates"]]
    assert hosts == ["accounts.google.com"]
