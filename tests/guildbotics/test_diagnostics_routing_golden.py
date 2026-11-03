"""Both hosts route the same diagnostics records to the execution index.

Regenerate with ``uv run --no-sync python -m
tests.guildbotics.test_diagnostics_routing_golden``. Expected decisions come
from Python, including its current exclusion of review-limit events.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.observability.session_transcripts import (
    INDEX_EVENT_TYPES,
    _belongs_in_index,
)

FIXTURE = (
    Path(__file__).resolve().parents[2] / "records/golden/diagnostics-routing.json"
)


def _cases():
    types = sorted(
        INDEX_EVENT_TYPES
        | {
            "review_limit.started",
            "review_limit.finished",
            "review_limit.failed",
            "review_limit.deferred",
            "session.pointer",
            "system.started",
            "system.finished",
            "system.custom",
            "provider.finished",
            "unknown.event",
        }
    )
    for kind in ("event", "log", "io", "memory"):
        for event_type in types:
            record = {
                "kind": kind,
                "type": event_type,
                "trace_id": "routing",
                "timestamp": "2026-11-03T00:00:00+00:00",
                "payload": {},
            }
            yield {"record": record, "indexed": _belongs_in_index(record)}


def test_fixture_covers_the_current_index_catalogue():
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == list(_cases())


@pytest.mark.parametrize(
    "case",
    [] if __name__ == "__main__" else json.loads(FIXTURE.read_text(encoding="utf-8")),
    ids=lambda case: f"{case['record']['kind']}:{case['record']['type']}",
)
def test_python_routes_the_shared_fixture(case, tmp_path):
    index = tmp_path / "diagnostics.jsonl"
    store = DiagnosticsStore(index)
    store.record(case["record"])
    records = [json.loads(line) for line in index.read_text().splitlines()]
    expected = ["session.pointer"]
    if case["indexed"]:
        expected.append(case["record"]["type"])
    assert [record["type"] for record in records] == expected
    transcript = tmp_path / "sessions/routing.jsonl"
    [saved] = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert (saved["kind"], saved["type"]) == (
        case["record"]["kind"],
        case["record"]["type"],
    )


if __name__ == "__main__":
    FIXTURE.write_text(
        json.dumps(list(_cases()), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
