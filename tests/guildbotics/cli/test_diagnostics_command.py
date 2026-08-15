"""Tests for the read-only ``guildbotics diagnostics`` commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from guildbotics.cli import main
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT


def _record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": "event",
        "type": "command.finished",
        "trace_id": "trace-ok",
        "span_id": "span-1",
        "source": "routine",
        "person_id": "aiko",
        "command": "reports/weekly",
        "workflow": "",
        "attributes": {},
        "payload": {},
        "timestamp": "2026-07-20T10:00:00+09:00",
    }
    record.update(overrides)
    return record


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a workspace data root with an index and two transcripts."""
    run_dir = tmp_path / ".guildbotics" / "local" / "run"
    sessions = run_dir / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    index = [
        _record(),
        _record(
            trace_id="trace-bad",
            type="command.failed",
            command="git/push",
            timestamp="2026-07-21T10:00:00+09:00",
        ),
        _record(
            trace_id="trace-assist",
            command="troubleshoot:trace-bad",
            source="manual",
            timestamp="2026-07-22T10:00:00+09:00",
        ),
    ]
    (run_dir / "diagnostics.jsonl").write_text(
        "".join(f"{json.dumps(item, ensure_ascii=False)}\n" for item in index),
        encoding="utf-8",
    )
    (sessions / "trace-bad.jsonl").write_text(
        "".join(
            f"{json.dumps(item, ensure_ascii=False)}\n"
            for item in (
                _record(trace_id="trace-bad", type="command.started"),
                _record(
                    trace_id="trace-bad",
                    kind="log",
                    type=None,
                    level="error",
                    message="gh: authentication required",
                ),
                _record(
                    trace_id="trace-bad",
                    kind="io",
                    type="cli_agent.response",
                    payload={"stderr": "fatal: could not read Username"},
                ),
            )
        ),
        encoding="utf-8",
    )
    return tmp_path


def _run(*args: str) -> dict[str, Any]:
    result = CliRunner().invoke(main, ["diagnostics", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_traces_lists_executions_newest_first(workspace: Path) -> None:
    payload = _run("traces")

    assert [trace["trace_id"] for trace in payload["traces"]] == [
        "trace-bad",
        "trace-ok",
    ]
    assert payload["traces"][0]["status"] == "failed"
    assert payload["traces"][0]["command"] == "git/push"


def test_traces_hides_assistant_traces_unless_asked(workspace: Path) -> None:
    hidden = {trace["trace_id"] for trace in _run("traces")["traces"]}
    shown = {
        trace["trace_id"] for trace in _run("traces", "--include-assistant")["traces"]
    }

    # The assistant must not investigate its own conversations.
    assert "trace-assist" not in hidden
    assert "trace-assist" in shown


def test_traces_filters_by_source_and_limit(workspace: Path) -> None:
    assert _run("traces", "--source", "nothing")["traces"] == []
    # The newest trace is an assistant turn, so the limit must apply after the
    # assistant filter rather than letting it consume the whole page.
    assert [
        trace["trace_id"] for trace in _run("traces", "--limit", "1")["traces"]
    ] == ["trace-bad"]


def test_traces_rejects_a_limit_outside_the_supported_range(workspace: Path) -> None:
    result = CliRunner().invoke(main, ["diagnostics", "traces", "--limit", "0"])

    assert result.exit_code != 0


def test_trace_returns_the_summary_and_full_transcript(workspace: Path) -> None:
    payload = _run("trace", "trace-bad")

    assert payload["summary"]["status"] == "failed"
    assert payload["record_count"] == 3
    assert {record["kind"] for record in payload["records"]} == {"event", "log", "io"}


def test_trace_filters_by_kind_and_level(workspace: Path) -> None:
    errors = _run("trace", "trace-bad", "--kind", "log", "--level", "error")["records"]
    io_records = _run("trace", "trace-bad", "--kind", "io")["records"]

    assert [record["message"] for record in errors] == ["gh: authentication required"]
    assert io_records[0]["payload"]["stderr"] == "fatal: could not read Username"


def test_trace_reports_an_unknown_execution_as_empty(workspace: Path) -> None:
    payload = _run("trace", "trace-missing")

    assert payload["summary"] is None
    assert payload["records"] == []


def test_system_returns_records_without_an_execution(workspace: Path) -> None:
    assert "records" in _run("system")


def test_workspace_option_selects_another_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    (other / ".guildbotics/config").mkdir(parents=True)
    run_dir = other / ".guildbotics/local/run"
    run_dir.mkdir(parents=True)
    (run_dir / "diagnostics.jsonl").write_text(
        json.dumps(_record(trace_id="other-trace")) + "\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    result = CliRunner().invoke(
        main, ["diagnostics", "--workspace", str(other), "traces"]
    )

    assert result.exit_code == 0, result.output
    assert [trace["trace_id"] for trace in json.loads(result.output)["traces"]] == [
        "other-trace"
    ]


def test_reading_diagnostics_never_writes_to_the_workspace(workspace: Path) -> None:
    before = {
        path: path.read_bytes()
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
    }

    _run("traces")
    _run("trace", "trace-bad")
    _run("system")

    after = {
        path: path.read_bytes()
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
    }
    assert after == before


def test_traces_query_searches_transcript_bodies(workspace: Path) -> None:
    # The failure text lives in the transcript, not the index, so a recurrence
    # search that only looked at the index would miss it entirely.
    payload = _run("traces", "--query", "authentication required")

    assert [trace["trace_id"] for trace in payload["traces"]] == ["trace-bad"]


def test_traces_query_searches_command_output_payloads(workspace: Path) -> None:
    payload = _run("traces", "--query", "could not read Username")

    assert [trace["trace_id"] for trace in payload["traces"]] == ["trace-bad"]


def test_traces_query_still_matches_summary_fields(workspace: Path) -> None:
    payload = _run("traces", "--query", "reports/weekly")

    assert [trace["trace_id"] for trace in payload["traces"]] == ["trace-ok"]


def test_traces_query_excludes_non_matching_executions(workspace: Path) -> None:
    assert _run("traces", "--query", "no such text anywhere")["traces"] == []
