"""Device-local last-turn readings shared by setup and activity."""

import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.cli_agent_turns import last_cli_agent_turns
from guildbotics.app_api.events import EventBus
from guildbotics.observability.diagnostics_store import DiagnosticsStore


def _record(
    store,
    *,
    person="alice",
    tool="codex",
    model="actual",
    effort="",
    specified=False,
    timestamp="2026-09-29T10:00:00Z",
    status="finished",
    slot="default",
):
    store.record(
        {
            "kind": "event",
            "type": f"span.{status}",
            "person_id": person,
            "timestamp": timestamp,
            "attributes": {
                "agent.kind": "cli_agent",
                "agent.adapter": tool,
                "agent.slot": slot,
            },
            "payload": {"model": model, "model_specified": specified, "effort": effort},
        }
    )


def test_latest_finished_or_failed_turn_per_member_and_tool(tmp_path):
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl", memory_limit=1)
    _record(store, model="old", effort="low")
    _record(store, tool="claude", model="sonnet", specified=True, effort="high")
    _record(store, person="bob", model="bob-model", effort="xhigh")
    # Unknown effective values on the latest turn must replace an older model.
    _record(
        store,
        model="",
        status="failed",
        slot="reviewer",
        timestamp="2026-09-29T12:00:00Z",
    )
    # Arrival order and spelling of the timezone must not decide recency.
    _record(store, model="late-arriving-old", timestamp="2026-09-29T19:30:00+09:00")
    _record(
        store, model="still-running", status="started", timestamp="2026-09-30T00:00:00Z"
    )

    turns = {(turn.person_id, turn.agent): turn for turn in last_cli_agent_turns(store)}
    assert set(turns) == {("alice", "codex"), ("alice", "claude"), ("bob", "codex")}
    assert turns["alice", "codex"].model == ""
    assert turns["alice", "codex"].effort == ""
    assert turns["alice", "codex"].timestamp == "2026-09-29T12:00:00Z"
    assert turns["alice", "claude"].model == "sonnet"
    assert turns["alice", "claude"].model_specified is True
    assert turns["alice", "claude"].effort == "high"
    assert turns["bob", "codex"].model == "bob-model"
    assert turns["bob", "codex"].model_specified is False
    assert turns["bob", "codex"].effort == "xhigh"


def test_no_store_or_no_records(tmp_path):
    assert last_cli_agent_turns(None) == []
    assert last_cli_agent_turns(DiagnosticsStore(tmp_path / "empty.jsonl")) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"person": ""},
        {"tool": ""},
        {"specified": None},
        {"timestamp": "invalid"},
    ],
)
def test_incomplete_turn_facts_are_not_inferred_from_current_settings(
    tmp_path, overrides
):
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    _record(store, **overrides)
    assert last_cli_agent_turns(store) == []


@pytest.mark.parametrize("effort", ["high", "xhigh", ""])
def test_api_reads_persisted_device_records_and_requires_token(tmp_path, effort):
    path = tmp_path / "diagnostics.jsonl"
    _record(DiagnosticsStore(path), specified=True, effort=effort)
    store = DiagnosticsStore(path)
    app = create_app(
        session_token="secret", event_bus=EventBus(store=store), diagnostics_store=store
    )
    with TestClient(app) as client:
        url = "/intelligences/cli-agents/last-turns"
        assert client.get(url).status_code == 401
        response = client.get(url, headers={"X-GuildBotics-Session-Token": "secret"})
    assert response.status_code == 200
    assert response.json() == [
        {
            "person_id": "alice",
            "agent": "codex",
            "model": "actual",
            "model_specified": True,
            "effort": effort,
            "timestamp": "2026-09-29T10:00:00Z",
        }
    ]
