import json
from pathlib import Path

from guildbotics.observability import span_scope, trace_scope
from guildbotics.utils.prompt_trace import write_prompt_trace


def test_prompt_trace_writes_jsonl_only_when_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    trace_path = tmp_path / "prompt_trace.jsonl"
    monkeypatch.delenv("GUILDBOTICS_PROMPT_TRACE", raising=False)
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE_PATH", str(trace_path))

    write_prompt_trace("llm.request", {"person_id": "alice", "prompt": "hello"})
    assert not trace_path.exists()

    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE", "1")
    write_prompt_trace(
        "llm.request",
        {
            "person_id": "alice",
            "prompt": "hello",
            "cwd": tmp_path,
            "items": [{"nested": True}],
        },
    )

    events = [
        json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["event"] == "llm.request"
    assert events[0]["person_id"] == "alice"
    assert events[0]["prompt"] == "hello"
    assert events[0]["cwd"] == str(tmp_path)
    assert events[0]["items"] == [{"nested": True}]


def test_prompt_trace_attaches_correlation_ids(tmp_path: Path, monkeypatch) -> None:
    trace_path = tmp_path / "prompt_trace.jsonl"
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE", "1")
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE_PATH", str(trace_path))

    with trace_scope("manual", trace_id="trace-1"):
        with span_scope("llm"):
            write_prompt_trace("llm.request", {"prompt": "hello"})
            write_prompt_trace("llm.response", {"content": "hi"})

    events = [
        json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["trace_id"] == "trace-1"
    assert events[0]["source"] == "manual"
    # request and response within one span share a call_id (and span_id).
    assert events[0]["call_id"] == events[1]["call_id"]
    assert events[0]["span_id"] == events[1]["span_id"]


def test_prompt_trace_without_trace_has_no_correlation(
    tmp_path: Path, monkeypatch
) -> None:
    trace_path = tmp_path / "prompt_trace.jsonl"
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE", "1")
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE_PATH", str(trace_path))

    write_prompt_trace("llm.request", {"prompt": "hello"})

    event = json.loads(trace_path.read_text(encoding="utf-8"))
    assert "trace_id" not in event


def test_prompt_trace_payload_cannot_override_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    trace_path = tmp_path / "prompt_trace.jsonl"
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE", "1")
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE_PATH", str(trace_path))

    write_prompt_trace(
        "llm.request",
        {"event": "spoofed", "timestamp": "spoofed", "person_id": "alice"},
    )

    event = json.loads(trace_path.read_text(encoding="utf-8"))
    assert event["event"] == "llm.request"
    assert event["timestamp"] != "spoofed"
    assert event["person_id"] == "alice"
