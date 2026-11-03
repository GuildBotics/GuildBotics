"""Shared records written by both hosts, using synthetic values only."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from guildbotics.app_api import trace_presentations
from guildbotics.app_api.agent_streams import collapse_assistant_streams

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.interactive_sessions import (
    InteractiveSessionStore,
    InteractiveTraceSession,
)

GOLDEN = Path(__file__).resolve().parents[2] / "records/golden/shared-records.json"
NOW = datetime(2026, 11, 3, 10, tzinfo=UTC)


def capture(workspace):
    """Exercise the Python writers; the checked-in fixture is their answer."""
    state = workspace / ".guildbotics/state"
    records = []
    for kind in ("ticket.comment", "command.failed", "diagnostics.completed"):
        path = ActivityEventStore(state / "events", workspace_root=workspace).record(
            {
                "event_id": kind,
                "timestamp": "2026-11-01T00:30:00+14:00",
                "type": kind,
                "person_id": "p1",
                "payload": {
                    "nested": ["synthetic-secret in error", "界" * 501],
                    "stdout": "local only",
                },
                "attributes": {"new_field": "synthetic-secret"},
                "schema_version": 999,
            }
        )
        records.append(json.loads(path.read_text()))

    runs = RunStore(state / "task-runs", member_id="p1")
    runs.start_record(
        "run-1",
        work_kind="ticket",
        execution_mode="autonomous",
        member_id="p1",
        source="routine",
        attributes={"target.title": "synthetic-secret", "target.empty": ""},
        work_identity={"key": "synthetic-secret" + "x" * 501},
    )
    runs.append(
        "run-1",
        {
            "kind": "evidence",
            "evidence_type": "issue_comment",
            "payload": {"text": "synthetic-secret", "messages": ["local"]},
        },
    )
    runs.finish_record(
        "run-1",
        status="failed",
        safe_summary="synthetic-secret",
        attributes={
            "target.title": "later",
            "target.url": "ticket/1",
            "target.empty": "filled",
        },
    )
    [run] = runs.records()
    result = run.model_dump()
    for key in ("started_at", "finished_at"):
        result[key] = "<now>"
    for item in result["provider_evidence"]:
        item["recorded_at"] = "<now>"

    completions = []
    for kind in ("member-work", "ticket"):
        for status in ("done", "asking", "blocked", "awaiting_checks"):
            run_id = f"{kind}-{status}"
            runs.start_record(
                run_id, work_kind=kind, execution_mode="autonomous", member_id="p1"
            )
            runs.append(
                run_id,
                {"kind": "evidence", "evidence_type": "issue_comment", "payload": {}},
            )
            runs.append(
                run_id,
                {"kind": "evidence", "evidence_type": "checks_awaited", "payload": {}},
            )
            runs.complete_run(
                run_id,
                status,
                "completed",
                subject_type="ticket",
                subject_id="ticket/1",
                person_id="p1",
            )
            written = runs._read_record(run_id).model_dump()
            for key in ("started_at", "finished_at"):
                written[key] = "<now>"
            for item in written["provider_evidence"]:
                item["recorded_at"] = "<now>"
            completions.append(written)

    sessions = InteractiveSessionStore(state / "sessions")
    session = InteractiveTraceSession(
        trace_id="trace-1",
        person_id="p1",
        workspace="/workspace",
        host="codex",
        thread_key="thread",
        started_at=NOW.isoformat(),
        last_seen_at=NOW.isoformat(),
        expires_at="2026-11-03T10:30:00+00:00",
    )
    sessions.record(
        session,
        command="member ticket read",
        status="success",
        now=NOW,
        attributes={"target.title": "synthetic-secret", "local.path": "local"},
    )
    shared = sessions.record(
        session,
        command="member ticket comment",
        status="failed",
        now=NOW,
        attributes={"target.title": "later", "target.url": "ticket/1"},
    )
    return {
        "events": records,
        "run": result,
        "completions": completions,
        "session": shared,
        "presentations": presentations(),
        "streams": streams(),
    }


def presentations():
    events = set(trace_presentations._EXACT_EVENT_LABELS) | set(
        trace_presentations._TARGET_LABELS
    )
    events.update(f"agent_runtime.{k}" for k in trace_presentations._AGENT_LABELS)
    events.update(
        f"{prefix}.{phase}"
        for prefix in ("command", "member.command")
        for phase in trace_presentations._COMMAND_PHASES
    )
    events.update(
        f"{prefix}.{phase}"
        for prefix in ("scheduler", "events")
        for phase in trace_presentations._SERVICE_PHASES
    )
    events.update(("span.finished", "span.failed", "future.failed", "future.event"))
    cases = []
    for event in sorted(events):
        raw = {
            "kind": "event",
            "type": event,
            "command": "command",
            "payload": {
                "name": "completed",
                "model": "model",
                "duration_ms": 1234,
                "run_id": "run",
                "attempt": 2,
                "max_attempts": 4,
                "retry_after_text": "later",
                "provider": "cli_agent",
                "cli_agent": "codex",
                "checks": [True],
                "ok": True,
                "path": "sessions/trace.jsonl",
                "candidates": [{"destination": "example.test", "port": 443}],
                "title": "Title",
                "ref": "refs/heads/main",
            },
            "attributes": {"target.label": "#1", "service_run_id": "service"},
        }
        cases.append(raw)
    cases.extend(
        {"kind": "log", "level": level, "message": "logged"}
        for level in ("INFO", "WARNING", "ERROR")
    )
    cases.extend(
        {"kind": "memory", "type": f"memory.{action}", "message": "memory"}
        for action in ("record", "recall", "archive", "touch")
    )
    cases.extend(
        {
            "kind": "io",
            "type": event,
            "payload": {"output": "result", "effort": {"resolved": "high"}},
        }
        for event in ("command.output", "cli_agent.request", "future.io")
    )
    return [
        {
            "raw": raw,
            "expected": trace_presentations.normalize_trace_presentation(
                raw
            ).model_dump(),
        }
        for raw in cases
    ]


def streams():
    cases = []
    for names in (
        ("started", "delta", "delta", "completed"),
        ("started", "delta", "delta"),
        ("thinking", "thinking", "completed"),
        ("started",),
    ):
        raw = [
            {
                "kind": "event",
                "type": "agent_runtime.assistant",
                "span_id": "span",
                "timestamp": str(i),
                "payload": {"name": name, "message": str(i)},
            }
            for i, name in enumerate(names)
        ]
        cases.append({"raw": raw, "expected": collapse_assistant_streams(raw)})
    return cases


def test_shared_records_match_the_python_writers(tmp_path):
    with (
        patch(
            "guildbotics.utils.shared_redaction.stored_secret_values",
            return_value=("synthetic-secret",),
        ),
        patch("guildbotics.capabilities.task_runs._device_id", return_value="device-1"),
    ):
        actual = capture(tmp_path)
    if os.environ.get("RECORD_SHARED_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n")
    assert actual == json.loads(GOLDEN.read_text())
