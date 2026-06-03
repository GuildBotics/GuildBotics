import json
from pathlib import Path

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
