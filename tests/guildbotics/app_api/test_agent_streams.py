"""Tests for assistant stream collapsing in trace displays."""

from __future__ import annotations

from typing import Any

from guildbotics.app_api.agent_streams import collapse_assistant_streams
from guildbotics.intelligences.agent_runtime.diagnostics import MAX_MESSAGE


def _assistant(name: str, message: str = "", span_id: str = "span-1") -> dict[str, Any]:
    return {
        "kind": "event",
        "type": "agent_runtime.assistant",
        "span_id": span_id,
        "payload": {"name": name, "message": message},
    }


def _event(event_type: str) -> dict[str, Any]:
    return {"kind": "event", "type": event_type, "payload": {}}


def test_completed_stream_drops_started_and_deltas() -> None:
    records = [
        _event("command.started"),
        _assistant("started"),
        _assistant("delta", "Hel"),
        _assistant("delta", "lo"),
        _assistant("completed", "Hello"),
        _event("command.finished"),
    ]

    collapsed = collapse_assistant_streams(records)

    assert [item["type"] for item in collapsed] == [
        "command.started",
        "agent_runtime.assistant",
        "command.finished",
    ]
    assert collapsed[1]["payload"] == {"name": "completed", "message": "Hello"}


def test_interrupted_stream_collapses_into_one_partial_record() -> None:
    records = [
        _assistant("started"),
        _assistant("delta", "partial "),
        _assistant("delta", "output"),
        _event("span.failed"),
    ]

    collapsed = collapse_assistant_streams(records)

    assert [item["type"] for item in collapsed] == [
        "agent_runtime.assistant",
        "span.failed",
    ]
    assert collapsed[0]["payload"] == {
        "name": "partial",
        "message": "partial output",
        "partial": True,
    }


def test_lone_started_without_deltas_is_left_untouched() -> None:
    records = [
        _assistant("started"),
        _event("span.failed"),
    ]

    collapsed = collapse_assistant_streams(records)

    assert collapsed == records


def test_partial_message_is_capped() -> None:
    records = [
        _assistant("delta", "a" * 5000),
        _assistant("delta", "b" * 5000),
    ]

    collapsed = collapse_assistant_streams(records)

    assert len(collapsed) == 1
    message = collapsed[0]["payload"]["message"]
    assert len(message) == MAX_MESSAGE
    assert message.startswith("a")


def test_streams_are_tracked_per_span() -> None:
    records = [
        _assistant("delta", "first", span_id="span-1"),
        _assistant("delta", "second", span_id="span-2"),
        _assistant("completed", "first done", span_id="span-1"),
    ]

    collapsed = collapse_assistant_streams(records)

    assert [item["payload"]["message"] for item in collapsed] == [
        "second",
        "first done",
    ]
    assert collapsed[0]["payload"]["partial"] is True


def test_sequential_streams_in_one_span_collapse_independently() -> None:
    records = [
        _assistant("delta", "one"),
        _assistant("completed", "one done"),
        _assistant("delta", "two"),
        _assistant("completed", "two done"),
    ]

    collapsed = collapse_assistant_streams(records)

    assert [item["payload"]["message"] for item in collapsed] == [
        "one done",
        "two done",
    ]


def test_non_assistant_records_pass_through_unchanged() -> None:
    records = [
        _event("agent_runtime.tool"),
        {"kind": "io", "type": "cli_agent.request", "payload": {"message": "hi"}},
        {"kind": "log", "level": "INFO", "message": "line"},
    ]

    assert collapse_assistant_streams(records) == records
