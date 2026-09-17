"""The activity timeline and the diagnostics trace list agree on one trace.

Both screens answer the same question about the same records, so they read
one implementation. Before that, each folded the events itself and the two
folds had already drifted apart (only one of them knew about rate limits and
error logs).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from guildbotics.app_api.activity_history import build_activity_history
from guildbotics.entities.team import Person
from guildbotics.observability.diagnostics_store import DiagnosticsStore

TRACE_ID = "t-chat"
START = datetime(2026, 7, 1, tzinfo=UTC)
END = datetime(2026, 7, 2, tzinfo=UTC)

#: One Slack-triggered chat run. The github attributes are what puts an
#: automated workflow on the activity timeline at all, so every stage keeps
#: them and the two screens compare the same trace.
_BASE: dict[str, Any] = {
    "kind": "event",
    "type": "command.started",
    "trace_id": TRACE_ID,
    "person_id": "alice",
    "timestamp": "2026-07-01T10:00:00+00:00",
    "source": "event_listener",
    "command": "workflows/chat_conversation_workflow",
    "attributes": {
        "slack.channel": "C1",
        "slack.thread_ts": "100.1",
        "github.url": "https://github.com/o/r/issues/42",
        "github.number": "42",
        "github.kind": "issue",
    },
    "payload": {"command": "workflows/chat_conversation_workflow"},
}


def _stage(*event_types: str) -> list[dict[str, Any]]:
    records = [dict(_BASE)]
    for offset, event_type in enumerate(event_types, start=1):
        record = dict(_BASE)
        record.update(
            type=event_type,
            timestamp=f"2026-07-01T10:00:{offset:02d}+00:00",
            payload={},
        )
        records.append(record)
    return records


STAGES: dict[str, list[dict[str, Any]]] = {
    "llm decision finished, agent turn still running": _stage("span.finished"),
    "command boundary closed": _stage("span.finished", "command.finished"),
    "workflow completion recorded": _stage(
        "span.finished", "command.finished", "workflow.completed"
    ),
    "completion missing, retry scheduled": _stage(
        "command.finished",
        "workflow.completion_missing",
        "chat_dispatch.retry_scheduled",
    ),
    "rate limited": _stage("workflow.rate_limited"),
    "provider span failed": _stage("span.failed"),
}


def _timeline_status(records: list[dict[str, Any]]) -> str:
    history = build_activity_history(
        start=START,
        end=END,
        members=[
            Person(person_id="alice", name="Alice", person_type="agent", is_active=True)
        ],
        records=records,
    )
    assert len(history.sessions) == 1
    return str(history.sessions[0].status)


def _diagnostics_status(records: list[dict[str, Any]], path: Path) -> str:
    store = DiagnosticsStore(path)
    for item in records:
        store.record(item)
    return str(store.get_summary(TRACE_ID)["status"])


@pytest.mark.parametrize("stage", sorted(STAGES), ids=sorted(STAGES))
def test_both_screens_report_the_same_status(stage: str, tmp_path: Path) -> None:
    records = STAGES[stage]
    assert _timeline_status(records) == _diagnostics_status(
        records, tmp_path / "diag.jsonl"
    )


def test_running_chat_workflow_is_not_shown_as_success(tmp_path: Path) -> None:
    records = STAGES["llm decision finished, agent turn still running"]
    assert _timeline_status(records) == "running"
    assert _diagnostics_status(records, tmp_path / "diag.jsonl") == "running"
