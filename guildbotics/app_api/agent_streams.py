"""Display normalization for AI agent streaming records."""

from __future__ import annotations

from typing import Any


def collapse_assistant_streams(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse ``agent_runtime.assistant`` streaming records for display.

    Adapters guarantee that a completed assistant stream ends with a
    ``completed`` event carrying the full response text, so the preceding
    ``started``/``delta`` records are redundant and dropped. A stream that
    never completed — the AI CLI call is still running or was interrupted
    mid-stream — is collapsed into a single partial-response record so the
    partial output stays visible on the timeline.
    """
    dropped: set[int] = set()
    replaced: dict[int, dict[str, Any]] = {}
    pending: dict[str, list[int]] = {}
    for index, item in enumerate(records):
        if str(item.get("type") or "") != "agent_runtime.assistant":
            continue
        name = str(_payload(item).get("name") or "")
        stream = str(item.get("span_id") or "")
        if name in {"started", "delta"}:
            pending.setdefault(stream, []).append(index)
        elif name == "completed":
            dropped.update(pending.pop(stream, []))
    for indexes in pending.values():
        last = indexes[-1]
        dropped.update(indexes[:-1])
        message = "".join(
            str(_payload(records[index]).get("message") or "") for index in indexes
        )
        replaced[last] = {
            **records[last],
            "payload": {"name": "partial", "message": message, "partial": True},
        }
    return [
        replaced.get(index, item)
        for index, item in enumerate(records)
        if index not in dropped
    ]


def _payload(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("payload")
    return value if isinstance(value, dict) else {}
