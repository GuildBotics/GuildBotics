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


def test_reasoning_chunks_are_collapsed_into_one_record() -> None:
    records = [
        _assistant("thinking", "The "),
        _assistant("thinking", "user "),
        _assistant("thinking", "wants "),
        _assistant("thinking", "a path."),
        _assistant("delta", "OK"),
        _assistant("completed", "OK"),
    ]

    collapsed = collapse_assistant_streams(records)

    assert [_payload(item)["name"] for item in collapsed] == ["thinking", "completed"]
    assert _payload(collapsed[0])["message"] == "The user wants a path."


def test_reasoning_and_reply_streams_do_not_merge() -> None:
    records = [
        _assistant("thinking", "reasoning text"),
        _assistant("delta", "reply "),
        _assistant("delta", "text"),
    ]

    collapsed = collapse_assistant_streams(records)

    messages = {_payload(item)["name"]: _payload(item)["message"] for item in collapsed}
    assert messages["thinking"] == "reasoning text"
    assert messages["partial"] == "reply text"


def test_reasoning_streams_are_separated_by_span() -> None:
    records = [
        _assistant("thinking", "first", span_id="span-1"),
        _assistant("thinking", "second", span_id="span-2"),
    ]

    collapsed = collapse_assistant_streams(records)

    assert [_payload(item)["message"] for item in collapsed] == ["first", "second"]


def test_a_single_reasoning_record_is_left_alone() -> None:
    records = [_assistant("thinking", "only one")]

    assert collapse_assistant_streams(records) == records


def _payload(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("payload")
    return value if isinstance(value, dict) else {}


def test_reasoning_runs_split_by_a_reply_keep_their_place() -> None:
    records = [
        _assistant("thinking", "first "),
        _assistant("thinking", "block"),
        _assistant("delta", "answer"),
        _assistant("thinking", "second "),
        _assistant("thinking", "block"),
    ]

    collapsed = collapse_assistant_streams(records)

    # Early reasoning must not be moved into the later run's record.
    assert [_payload(item)["message"] for item in collapsed] == [
        "first block",
        "answer",
        "second block",
    ]


def test_reasoning_separated_by_a_tool_record_is_not_merged_across_it() -> None:
    records = [
        _assistant("thinking", "before"),
        _event("agent_runtime.command"),
        _assistant("thinking", "after"),
    ]

    collapsed = collapse_assistant_streams(records)

    assert [_payload(item).get("message", "") for item in collapsed] == [
        "before",
        "",
        "after",
    ]
