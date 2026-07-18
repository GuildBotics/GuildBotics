"""Unit tests for ``guildbotics.observability.diagnostics_store``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
    assert summary["attributes"] == {
        "service_run_id": "svc-1",
        "session.path": "sessions/t1.jsonl",
    }


def test_list_traces_orders_mixed_offset_timestamps_by_instant(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "command.started", timestamp="2026-07-01T09:00:00+09:00"))
    store.record(_event("t1", "command.finished", timestamp="2026-07-01T00:30:00Z"))
    store.record(_event("t2", "command.started", timestamp="2026-07-01T00:15:00Z"))

    traces = store.list_traces()

    assert [trace["trace_id"] for trace in traces] == ["t2", "t1"]
    assert traces[1]["started_at"] == "2026-07-01T09:00:00+09:00"
    assert traces[1]["updated_at"] == "2026-07-01T00:30:00Z"


def test_list_traces_treats_naive_timestamp_as_utc(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "command.started", timestamp="2026-07-01T00:00:00"))
    store.record(_event("t2", "command.started", timestamp="2026-07-01T08:30:00+09:00"))

    traces = store.list_traces()

    assert [trace["trace_id"] for trace in traces] == ["t1", "t2"]


def test_failed_event_sets_failed_status_and_error_count(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "command.started"))
    store.record(_log("t1", "ERROR", "boom", "2026-06-12T00:00:05+09:00"))
    store.record(_event("t1", "command.failed", timestamp="2026-06-12T00:00:06+09:00"))

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
    store.start_system_session("service-1")
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
    values = [record.get("type") or record.get("message") for record in records]
    assert [value for value in values if value != "system.started"] == [
        "global",
        "scheduler.running",
    ]
    assert values.count("system.started") == 1


def test_global_records_orders_mixed_offset_timestamps_by_instant(
    tmp_path: Path,
) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.start_system_session("service-1")
    store.record(_event("", "scheduler.running", timestamp="2099-07-01T09:00:00+09:00"))
    store.record(_log(None, "INFO", "later", "2099-07-01T00:30:00Z"))

    records = store.global_records()

    values = [record.get("type") or record.get("message") for record in records]
    assert [value for value in values if value != "system.started"] == [
        "scheduler.running",
        "later",
    ]
    assert values.count("system.started") == 1
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


def test_records_after_reads_disk_rows_beyond_memory_window(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl", memory_limit=2)
    store.record(_event("t1", "command.failed"))
    for index in range(10):
        store.record(_log(None, "INFO", f"noise-{index}", "2026-07-01T00:00:00Z"))

    records, _ = store.records_after(
        None, includes=lambda item: item.get("kind") == "event"
    )

    assert [record["type"] for record in records] == [
        "session.pointer",
        "command.failed",
    ]


def test_records_after_returns_only_rows_appended_after_cursor(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "command.failed"))
    _, cursor = store.records_after(None, includes=lambda _: True)
    store.record(_event("t2", "command.finished"))

    records, next_cursor = store.records_after(cursor, includes=lambda _: True)

    assert [record["type"] for record in records] == [
        "session.pointer",
        "command.finished",
    ]
    assert next_cursor.offset > cursor.offset


def test_records_after_resets_cursor_when_file_is_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "diag.jsonl"
    store = DiagnosticsStore(path)
    store.record(_event("t1", "command.failed"))
    _, cursor = store.records_after(None, includes=lambda _: True)
    replacement = _event("t2", "scheduler.failed")
    path.write_text(json.dumps(replacement) + "\n", encoding="utf-8")

    records, _ = store.records_after(cursor, includes=lambda _: True)

    assert [record["type"] for record in records] == ["scheduler.failed"]


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


def test_default_store_migrates_legacy_diagnostics_and_prompt_trace_once(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_DATA_DIR", str(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    diagnostics = run_dir / "diagnostics.jsonl"
    prompt_trace = run_dir / "prompt_trace.jsonl"
    diagnostics.write_text('{"kind":"log","message":"legacy"}\n')
    prompt_trace.write_text('{"event":"llm.request"}\n')

    store = DiagnosticsStore()

    assert store.records_between(includes=lambda _timestamp: True) == []
    assert diagnostics.read_text() == ""
    assert prompt_trace.exists() is False
    assert (run_dir / ".session-transcripts-v1").is_file()

    diagnostics.write_text('{"kind":"event","type":"github.push"}\n')
    DiagnosticsStore()
    assert "github.push" in diagnostics.read_text()


def test_default_store_construction_does_not_create_workspace_files(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_DATA_DIR", str(tmp_path))

    store = DiagnosticsStore()

    assert store.path == tmp_path / "run/diagnostics.jsonl"
    assert (tmp_path / "run").exists() is False


def test_importing_diagnostics_events_does_not_mutate_cwd(tmp_path: Path) -> None:
    repository_root = Path(__file__).parents[3]
    env = os.environ.copy()
    env.pop("GUILDBOTICS_DATA_DIR", None)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(repository_root), env.get("PYTHONPATH", "")])
    )

    result = subprocess.run(
        [sys.executable, "-c", "import guildbotics.observability.diagnostics_events"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".guildbotics").exists() is False


def test_default_store_recovers_stale_migration_lock(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_DATA_DIR", str(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock = run_dir / ".session-transcripts-v1.lock"
    lock.write_text("999999999\n", encoding="utf-8")
    diagnostics = run_dir / "diagnostics.jsonl"
    diagnostics.write_text('{"kind":"log","message":"legacy"}\n')

    store = DiagnosticsStore()
    records = store.records_between(includes=lambda _timestamp: True)

    assert records == []
    assert (run_dir / ".session-transcripts-v1").is_file()
    assert lock.exists() is False


def test_reads_pick_up_records_appended_by_another_process(tmp_path: Path) -> None:
    # A long-lived reader (e.g. the app_api backend) and a separate writer
    # (e.g. a ``guildbotics member`` CLI subprocess) share one JSONL file. The
    # reader must reflect rows the writer appended after the reader loaded,
    # instead of serving its stale in-memory snapshot.
    path = tmp_path / "diag.jsonl"
    reader = DiagnosticsStore(path)
    writer = DiagnosticsStore(path)

    writer.record(_event("t1", "github.push", timestamp="2026-07-02T22:40:00+09:00"))

    records = reader.records_between(includes=lambda _timestamp: True, limit=10)
    assert [record["trace_id"] for record in records] == ["t1"]
    assert [record["type"] for record in records] == ["github.push"]
    assert reader.list_traces()[0]["trace_id"] == "t1"


def test_reads_reflect_external_rotation(tmp_path: Path) -> None:
    # When another process rotates the file (rewrite shrinks it), the reader
    # must reload rather than keep its larger stale snapshot.
    path = tmp_path / "diag.jsonl"
    reader = DiagnosticsStore(path)
    reader.record(_event("t1", "command.started"))
    assert reader.list_traces()[0]["trace_id"] == "t1"

    writer = DiagnosticsStore(path, max_file_bytes=1)
    writer.record(_event("t2", "command.started"))

    trace_ids = {summary["trace_id"] for summary in reader.list_traces()}
    assert "t2" in trace_ids


def test_record_with_non_serializable_attribute_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "diag.jsonl"
    store = DiagnosticsStore(path)
    # Path / set are not natively JSON-serializable; recording and querying must
    # coerce them (default=str) rather than crash the runtime.
    store.record(_event("t1", "command.started", attributes={"cwd": tmp_path}))

    traces = store.list_traces(query=tmp_path.name)
    assert [s["trace_id"] for s in traces] == ["t1"]
    assert DiagnosticsStore(path).list_traces()[0]["trace_id"] == "t1"


def test_finished_span_with_missing_completion_is_not_success(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "command.started"))
    store.record(_event("t1", "span.finished", timestamp="2026-06-12T00:00:02+09:00"))
    store.record(
        _event(
            "t1",
            "workflow.completion_missing",
            timestamp="2026-06-12T00:00:03+09:00",
            payload={"run_id": "run-1", "attempt": 1, "max_attempts": 2},
        )
    )
    store.record(
        _event(
            "t1",
            "chat_dispatch.retry_scheduled",
            timestamp="2026-06-12T00:00:04+09:00",
            payload={"next_attempt_at": "2026-06-12T00:05:00+09:00"},
        )
    )

    summary = store.list_traces()[0]
    assert summary["status"] == "retry_scheduled"


def test_missing_completion_without_dispatch_event_is_incomplete(
    tmp_path: Path,
) -> None:
    # The ticket workflow records ``workflow.completion_missing`` on the same
    # completion layer as chat, but exhausts attempts by posting an error
    # comment instead of a ``chat_dispatch`` event. Without that dispatch
    # event, "missing" alone must not read as "retry_scheduled".
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "command.started"))
    store.record(_event("t1", "span.finished", timestamp="2026-06-12T00:00:02+09:00"))
    store.record(
        _event(
            "t1",
            "workflow.completion_missing",
            timestamp="2026-06-12T00:00:03+09:00",
            payload={"run_id": "run-1", "attempt": 3, "max_attempts": 3},
        )
    )

    summary = store.list_traces()[0]
    assert summary["status"] == "incomplete"


def test_abandoned_dispatch_overrides_provider_span_success(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "span.finished"))
    store.record(
        _event(
            "t1",
            "chat_dispatch.abandoned",
            timestamp="2026-06-12T00:00:02+09:00",
            payload={"run_id": "run-1"},
        )
    )

    summary = store.list_traces()[0]
    assert summary["status"] == "abandoned"


def test_recorded_completion_after_missing_resolves_to_success(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "span.finished"))
    store.record(
        _event(
            "t1",
            "workflow.completion_missing",
            timestamp="2026-06-12T00:00:02+09:00",
        )
    )
    store.record(
        _event("t1", "workflow.completed", timestamp="2026-06-12T00:00:03+09:00")
    )

    summary = store.list_traces()[0]
    assert summary["status"] == "success"


def test_recorded_completion_overrides_earlier_failed_attempt(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    store.record(_event("t1", "cli_agent.failed"))
    store.record(
        _event("t1", "workflow.completed", timestamp="2026-06-12T00:00:02+09:00")
    )

    summary = store.list_traces()[0]
    assert summary["status"] == "success"
