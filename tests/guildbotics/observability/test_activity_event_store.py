import json
from datetime import UTC, datetime

from guildbotics.observability.activity_event_store import (
    ActivityEventStore,
    is_domain_activity_event,
)


def test_is_domain_activity_event() -> None:
    assert is_domain_activity_event("github.push") is True
    assert is_domain_activity_event("workflow.completed") is True
    assert is_domain_activity_event("command.started") is True
    assert is_domain_activity_event("span.finished") is False
    assert is_domain_activity_event("session.pointer") is False
    # Device-health diagnostics never enter the shared store.
    assert is_domain_activity_event("credential.failed") is False
    assert is_domain_activity_event("diagnostics.completed") is False
    assert is_domain_activity_event("verify.completed") is False
    assert is_domain_activity_event("scheduler.worker.failed") is False
    # Unknown lifecycle-looking types must opt in explicitly — including new
    # github.* events, which could carry raw provider responses.
    assert is_domain_activity_event("workflow.started") is False
    assert is_domain_activity_event("github.debug") is False


def test_activity_event_store_writes_one_file_per_event(tmp_path) -> None:
    store = ActivityEventStore(tmp_path / "events")
    path = store.record(
        {
            "type": "github.push",
            "timestamp": "2026-08-10T09:00:00+00:00",
            "person_id": "aiko",
            "payload": {"action": "push"},
            "attributes": {"github.url": "https://example.test"},
            "trace_id": "trace-1",
        }
    )
    assert path.parent.parent.parent == tmp_path / "events"
    events = store.list_between(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 31, tzinfo=UTC),
    )
    assert len(events) == 1
    assert events[0]["kind"] == "github.push"
    assert events[0]["member_id"] == "aiko"
    assert events[0]["local_trace_id"] == "trace-1"
    assert "prompt" not in events[0]
    records = store.records_between(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 31, tzinfo=UTC),
    )
    assert records[0]["type"] == "github.push"
    assert records[0]["payload"]["action"] == "push"


def test_record_enforces_shared_boundary_guarantees(
    tmp_path, monkeypatch, fake_keyring
):
    """The shared boundary masks known secret values and bounds sizes while
    keeping error text available for cross-machine troubleshooting."""
    from guildbotics.utils.secret_store import KeyringSecretStore

    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    KeyringSecretStore(tmp_path / ".guildbotics" / "config").set(
        "OPENAI_API_KEY", "sk-live-secret-12345"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-secret-12345")
    store = ActivityEventStore(tmp_path / "events")

    path = store.record(
        {
            "type": "chat_dispatch.abandoned",
            "timestamp": "2026-07-10T01:00:00Z",
            "payload": {
                "run_id": "run-1",
                "error": "codex exited with code 1: bad key sk-live-secret-12345"
                + "x" * 1000,
                "error_category": "failed",
                "stdout": "full stdout",
                "stderr": "full stderr",
                "nested": {"stderr": "x", "note": "sk-live-secret-12345 again"},
            },
        }
    )

    event = json.loads(path.read_text(encoding="utf-8"))
    payload = event["payload"]
    # Bulk log bodies stay local.
    assert "stdout" not in payload
    assert "stderr" not in payload
    assert "stderr" not in payload["nested"]
    # Error text IS shared (remote troubleshooting), but masked and bounded.
    assert payload["error"].startswith("codex exited with code 1: bad key ***")
    assert "sk-live-secret-12345" not in json.dumps(event)
    assert len(payload["error"]) <= 500
    assert payload["nested"]["note"] == "*** again"
    assert payload["error_category"] == "failed"
    assert payload["run_id"] == "run-1"
