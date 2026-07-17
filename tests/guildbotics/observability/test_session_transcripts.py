from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from guildbotics.observability.session_transcripts import (
    STANDARD_STDERR_TAIL_BYTES,
    SessionTranscriptStore,
    should_record_agent_event,
    standard_stderr_tail,
    transcript_detail,
    transcript_retention_days,
)


def _record(event_type: str, *, trace_id: str | None = "trace-1") -> dict:
    return {
        "kind": "event",
        "type": event_type,
        "trace_id": trace_id,
        "span_id": "span-1" if trace_id else None,
        "timestamp": "2026-07-17T00:00:00Z",
        "source": "interactive" if trace_id else "system",
        "person_id": "alice" if trace_id else "",
        "command": "sample" if trace_id else "",
        "workflow": "",
        "attributes": {},
        "payload": {},
    }


def test_trace_routes_full_records_to_session_and_summary_to_index(
    tmp_path: Path,
) -> None:
    store = SessionTranscriptStore(tmp_path / "run/diagnostics.jsonl")

    started = store.route(_record("command.started"))
    io_record = {
        **_record("llm.request"),
        "kind": "io",
        "payload": {"prompt": "full prompt"},
    }
    io_route = store.route(io_record)
    error_route = store.route(_record("agent_runtime.error"))
    finished = store.route(_record("command.finished"))

    assert [item["type"] for item in started.index_records] == [
        "session.pointer",
        "command.started",
    ]
    assert io_route.index_records == []
    assert error_route.index_records == []
    assert [item["type"] for item in finished.index_records] == ["command.finished"]
    exists, records = store.trace_records("trace-1")
    assert exists is True
    assert [item["kind"] for item in records] == [
        "event",
        "io",
        "event",
        "event",
    ]
    assert records[-1]["payload"] == {
        "event_count": 3,
        "log_count": 0,
        "error_count": 1,
        "span_count": 1,
    }


def test_system_sessions_are_split_and_keep_service_run_id(tmp_path: Path) -> None:
    store = SessionTranscriptStore(tmp_path / "run/diagnostics.jsonl")

    first = store.start_system_session("service-1")
    finished = store.finish_system_session()
    second = store.start_system_session("service-1")
    store.finish_system_session()

    first_id = first.index_records[0]["attributes"]["system_session_id"]
    second_id = second.index_records[0]["attributes"]["system_session_id"]
    assert first_id != second_id
    assert finished.index_records[0]["type"] == "system.finished"
    assert first.index_records[0]["attributes"]["service_run_id"] == "service-1"
    assert second.index_records[0]["attributes"]["service_run_id"] == "service-1"


def test_unscoped_records_do_not_implicitly_start_system_session(tmp_path: Path) -> None:
    store = SessionTranscriptStore(tmp_path / "run/diagnostics.jsonl")

    log_route = store.route(
        {
            **_record("", trace_id=None),
            "kind": "log",
            "level": "INFO",
            "message": "short-lived CLI log",
        }
    )
    event_route = store.route(_record("github.push", trace_id=None))

    assert log_route.index_records == []
    assert [item["type"] for item in event_route.index_records] == ["github.push"]
    assert list(store.sessions_dir.glob("*.jsonl")) == []


def test_transcript_settings_and_standard_filters(monkeypatch) -> None:
    monkeypatch.setenv("GUILDBOTICS_TRANSCRIPT_DETAIL", "full")
    monkeypatch.setenv("GUILDBOTICS_TRANSCRIPT_RETENTION_DAYS", "14")
    assert transcript_detail() == "full"
    assert transcript_retention_days() == 14
    assert should_record_agent_event("stream", "thinking.delta") is True

    monkeypatch.setenv("GUILDBOTICS_TRANSCRIPT_DETAIL", "standard")
    assert should_record_agent_event("stream", "thinking.delta") is False
    assert should_record_agent_event("tool", "completed") is True


def test_standard_stderr_keeps_last_eight_kibibytes() -> None:
    value = "x" * (STANDARD_STDERR_TAIL_BYTES + 100)
    result = standard_stderr_tail(value)
    assert result == "x" * STANDARD_STDERR_TAIL_BYTES


def test_prune_expired_keeps_active_system_session(tmp_path: Path, monkeypatch) -> None:
    store = SessionTranscriptStore(tmp_path / "run/diagnostics.jsonl")
    store.route(_record("command.started", trace_id="old-trace"))
    started = store.start_system_session("service-1")
    old_trace = store.trace_path("old-trace")
    system_name = started.index_records[0]["attributes"]["system_session_id"]
    system_path = store.sessions_dir / f"{system_name}.jsonl"
    old = (datetime.now(UTC) - timedelta(days=10)).timestamp()
    os.utime(old_trace, (old, old))
    os.utime(system_path, (old, old))
    monkeypatch.setenv("GUILDBOTICS_TRANSCRIPT_RETENTION_DAYS", "5")

    deleted = store.prune_expired()

    assert deleted == [old_trace]
    assert system_path.exists()
    store.finish_system_session()


def test_transcript_reader_skips_malformed_rows(tmp_path: Path) -> None:
    store = SessionTranscriptStore(tmp_path / "run/diagnostics.jsonl")
    path = store.trace_path("trace-1")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(_record("command.started")) + "\n{broken\n",
        encoding="utf-8",
    )

    exists, records = store.trace_records("trace-1")

    assert exists is True
    assert len(records) == 1
