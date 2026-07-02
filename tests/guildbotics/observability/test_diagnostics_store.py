"""Unit tests for ``guildbotics.observability.diagnostics_store``."""

from __future__ import annotations

from pathlib import Path

from guildbotics.observability.diagnostics_store import DiagnosticsStore

TWO_RECORDS = 2


def _event(trace_id: str, event_type: str, **fields: object) -> dict[str, object]:
    return {
        "kind": "event",
        "type": event_type,
        "trace_id": trace_id,
        "timestamp": fields.pop("timestamp", "2026-06-12T00:00:00+09:00"),
        "source": fields.pop("source", "manual"),
        "person_id": fields.pop("person_id", ""),
        "command": fields.pop("command", ""),
        "attributes": fields.pop("attributes", {}),
        "payload": fields.pop("payload", {}),
    }


def _log(trace_id: str | None, level: str, message: str, ts: str) -> dict[str, object]:
    return {
        "kind": "log",
        "level": level,
        "message": message,
        "trace_id": trace_id,
        "timestamp": ts,
    }


def test_list_traces_aggregates_records(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(
        _event(
            "t1",
            "command.started",
            command="demo",
            person_id="alice",
            timestamp="2026-06-12T00:00:01+09:00",
            attributes={"service_run_id": "svc-1"},
        )
    )
    store.record(_log("t1", "INFO", "working", "2026-06-12T00:00:02+09:00"))
    store.record(
        _event("t1", "command.finished", timestamp="2026-06-12T00:00:03+09:00")
    )

    traces = store.list_traces()
    assert len(traces) == 1
    summary = traces[0]
    assert summary["trace_id"] == "t1"
    assert summary["source"] == "manual"
    assert summary["command"] == "demo"
    assert summary["person_id"] == "alice"
    assert summary["status"] == "success"
    assert summary["event_count"] == TWO_RECORDS
    assert summary["log_count"] == 1
    assert summary["started_at"] == "2026-06-12T00:00:01+09:00"
    assert summary["updated_at"] == "2026-06-12T00:00:03+09:00"
    assert summary["attributes"] == {"service_run_id": "svc-1"}


def test_list_traces_orders_mixed_offset_timestamps_by_instant(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "command.started", timestamp="2026-07-01T09:00:00+09:00"))
    store.record(_event("t1", "command.finished", timestamp="2026-07-01T00:30:00Z"))
    store.record(_event("t2", "command.started", timestamp="2026-07-01T00:15:00Z"))

    traces = store.list_traces()

    assert [trace["trace_id"] for trace in traces] == ["t2", "t1"]
    assert traces[1]["started_at"] == "2026-07-01T09:00:00+09:00"
    assert traces[1]["updated_at"] == "2026-07-01T00:30:00Z"


def test_failed_event_sets_failed_status_and_error_count(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "command.started"))
    store.record(_event("t1", "command.failed"))
    store.record(_log("t1", "ERROR", "boom", "2026-06-12T00:00:05+09:00"))

    summary = store.list_traces()[0]
    assert summary["status"] == "failed"
    assert summary["error_count"] == TWO_RECORDS  # failed event + ERROR log


def test_list_traces_filters_by_source_person_and_query(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "command.started", source="manual", person_id="alice"))
    store.record(
        _event(
            "t2",
            "scheduler.running",
            source="routine",
            person_id="bob",
            command="workflows/ticket",
        )
    )

    assert {s["trace_id"] for s in store.list_traces(source="routine")} == {"t2"}
    assert {s["trace_id"] for s in store.list_traces(person_id="alice")} == {"t1"}
    assert {s["trace_id"] for s in store.list_traces(query="ticket")} == {"t2"}
    assert store.list_traces(source="nope") == []


def test_list_traces_filters_by_exact_attribute(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "command.started", attributes={"github.number": "42"}))
    store.record(_event("t2", "command.started", attributes={"github.number": "7"}))

    matched = store.list_traces(attr_key="github.number", attr_value="42")
    assert {s["trace_id"] for s in matched} == {"t1"}
    # A bare number does not fuzzy-match other traces.
    assert store.list_traces(attr_key="github.number", attr_value="1") == []


def test_global_records_returns_unscoped_events_and_logs(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_log(None, "INFO", "global", "2026-06-12T00:00:01+09:00"))
    store.record(
        _event(
            "",
            "scheduler.running",
            source="scheduler",
            timestamp="2026-06-12T00:00:02+09:00",
        )
    )
    store.record(_log("t1", "INFO", "scoped", "2026-06-12T00:00:03+09:00"))

    records = store.global_records()
    # Unscoped service events and global logs are returned (oldest-first);
    # records tied to a trace are excluded.
    assert [record.get("type") or record.get("message") for record in records] == [
        "global",
        "scheduler.running",
    ]


def test_global_records_orders_mixed_offset_timestamps_by_instant(
    tmp_path: Path,
) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("", "scheduler.running", timestamp="2026-07-01T09:00:00+09:00"))
    store.record(_log(None, "INFO", "later", "2026-07-01T00:30:00Z"))

    records = store.global_records()

    assert [record.get("type") or record.get("message") for record in records] == [
        "scheduler.running",
        "later",
    ]
    assert [record.get("message") for record in store.global_records(limit=1)] == [
        "later"
    ]


def test_get_records_returns_sorted_records_for_trace(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_log("t1", "INFO", "second", "2026-06-12T00:00:02+09:00"))
    store.record(_event("t1", "command.started", timestamp="2026-06-12T00:00:01+09:00"))
    records = store.get_records("t1")
    assert [r.get("timestamp") for r in records] == [
        "2026-06-12T00:00:01+09:00",
        "2026-06-12T00:00:02+09:00",
    ]


def test_get_records_orders_mixed_offset_timestamps_by_instant(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "command.finished", timestamp="2026-07-01T00:30:00Z"))
    store.record(_event("t1", "command.started", timestamp="2026-07-01T09:00:00+09:00"))

    records = store.get_records("t1")

    assert [record["timestamp"] for record in records] == [
        "2026-07-01T09:00:00+09:00",
        "2026-07-01T00:30:00Z",
    ]


def test_records_between_orders_and_limits_mixed_offset_timestamps_by_instant(
    tmp_path: Path,
) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "command.started", timestamp="2026-07-01T09:00:00+09:00"))
    store.record(_event("t2", "command.started", timestamp="2026-07-01T00:30:00Z"))

    records = store.records_between(includes=lambda _timestamp: True, limit=1)

    assert [record["trace_id"] for record in records] == ["t2"]


def test_records_persist_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "diag.jsonl"
    store = DiagnosticsStore(path)
    store.record(_event("t1", "command.started", command="demo"))

    reloaded = DiagnosticsStore(path)
    summary = reloaded.list_traces()[0]
    assert summary["trace_id"] == "t1"
    assert summary["command"] == "demo"


def test_rotation_does_not_duplicate_the_record_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "diag.jsonl"
    # Tiny budget so the next write triggers a rewrite-based rotation.
    store = DiagnosticsStore(path, max_file_bytes=1)
    store.record(_event("t1", "command.started", command="demo"))
    store.record(_event("t1", "command.finished"))

    # The rewrite already persists the in-memory window, so the rotated record
    # must appear exactly once — a fresh store must not double-count it.
    reloaded = DiagnosticsStore(path)
    summary = reloaded.list_traces()[0]
    assert summary["event_count"] == TWO_RECORDS
    assert path.read_text(encoding="utf-8").count('"command.finished"') == 1


def test_record_with_non_serializable_attribute_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "diag.jsonl"
    store = DiagnosticsStore(path)
    # Path / set are not natively JSON-serializable; recording and querying must
    # coerce them (default=str) rather than crash the runtime.
    store.record(_event("t1", "command.started", attributes={"cwd": tmp_path}))

    traces = store.list_traces(query=tmp_path.name)
    assert [s["trace_id"] for s in traces] == ["t1"]
    assert DiagnosticsStore(path).list_traces()[0]["trace_id"] == "t1"
