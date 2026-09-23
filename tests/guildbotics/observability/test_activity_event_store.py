import json
from datetime import UTC, datetime

from guildbotics.observability import activity_event_store as store_module
from guildbotics.observability.activity_event_store import (
    ActivityEventStore,
    is_domain_activity_event,
)


def test_is_domain_activity_event() -> None:
    assert is_domain_activity_event("github.push") is True
    assert is_domain_activity_event("workflow.completed") is True
    # A run's lifecycle is one shared record per run, not its boundary events:
    # those would be read once per command on every device, for nothing the
    # record does not already say.
    assert is_domain_activity_event("command.started") is False
    assert is_domain_activity_event("command.finished") is False
    assert is_domain_activity_event("member.command.started") is False
    assert is_domain_activity_event("member.command.failed") is False
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


def _months_read(store: ActivityEventStore, monkeypatch) -> list[str]:
    """Record which month directories ``store`` reads from here on."""
    read: list[str] = []
    real = store_module.iter_json_objects

    def _observed(directory, pattern):
        read.append(directory.relative_to(store.root).as_posix())
        return real(directory, pattern)

    monkeypatch.setattr(store_module, "iter_json_objects", _observed)
    return read


def test_list_between_reads_only_the_months_the_window_can_reach(
    tmp_path, monkeypatch
) -> None:
    store = ActivityEventStore(tmp_path / "events")
    for occurred in (
        "2026-06-30T23:00:00Z",
        "2026-07-15T09:00:00Z",
        "2026-09-01T00:00:00Z",
    ):
        store.record(
            {"type": "github.push", "timestamp": occurred, "person_id": "aiko"}
        )
    read = _months_read(store, monkeypatch)

    events = store.list_between(
        datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)
    )

    # The window's months that hold events, plus the neighbours one day of UTC
    # offset can reach.
    assert read == ["2026/06", "2026/07", "2026/09"]
    assert [item["occurred_at"] for item in events] == ["2026-07-15T09:00:00Z"]


def test_boundary_events_an_earlier_build_shared_are_not_read_as_facts(
    tmp_path,
) -> None:
    """A boundary event an earlier build shared is skipped, not read as a fact."""
    store = ActivityEventStore(tmp_path / "events")
    for index in range(5):
        store.record(
            {
                "type": "member.command.started",
                "timestamp": f"2026-08-10T09:00:{index:02d}+00:00",
                "person_id": "aiko",
            }
        )
    store.record({"type": "github.push", "timestamp": "2026-08-10T08:00:00+00:00"})

    events = store.list_between(
        datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)
    )

    assert [item["kind"] for item in events] == ["github.push"]


def test_list_between_finds_events_another_offset_filed_in_a_neighbouring_month(
    tmp_path,
) -> None:
    """An event is filed under the calendar month of its own offset; the window
    is asked in UTC. ``2026-08-31T13:00-12:00`` is ``2026-09-01T01:00Z`` and
    lives in ``2026/08``."""
    store = ActivityEventStore(tmp_path / "events")
    path = store.record(
        {"type": "github.push", "timestamp": "2026-08-31T13:00:00-12:00"}
    )
    assert path.parent.parent.name == "2026" and path.parent.name == "08"

    events = store.list_between(
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)
    )

    assert [item["occurred_at"] for item in events] == ["2026-08-31T13:00:00-12:00"]


def test_list_between_accepts_an_unbounded_window(tmp_path) -> None:
    # A caller asking for everything hands in the ends of the calendar; the
    # neighbouring-month reach must not step past them.
    store = ActivityEventStore(tmp_path / "events")
    store.record({"type": "github.push", "timestamp": "2026-08-10T08:00:00+00:00"})

    events = store.list_between(
        datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC)
    )

    assert [item["kind"] for item in events] == ["github.push"]


def test_an_unbounded_window_reads_only_the_months_the_store_holds(
    tmp_path, monkeypatch
) -> None:
    """Ten thousand years of calendar months are not ten thousand years of
    lookups: on Windows that alone took seconds per sync status read (#570)."""
    store = ActivityEventStore(tmp_path / "events")
    store.record({"type": "github.push", "timestamp": "2026-08-10T08:00:00+00:00"})
    store.record({"type": "github.push", "timestamp": "2027-01-10T08:00:00+00:00"})
    read = _months_read(store, monkeypatch)

    events = store.list_between(
        datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC)
    )

    assert len(events) == 2
    assert read == ["2026/08", "2027/01"]
