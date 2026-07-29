"""Display normalization for AI agent streaming records."""

from __future__ import annotations

from typing import Any

from guildbotics.intelligences.agent_runtime.diagnostics import MAX_MESSAGE

#: Fewest records in a stream worth merging into one.
_MERGEABLE = 2


def collapse_assistant_streams(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse ``agent_runtime.assistant`` streaming records for display.

    Adapters guarantee that a completed assistant stream ends with a
    ``completed`` event carrying the full response text, so the preceding
    ``started``/``delta`` records are redundant and dropped. A stream that
    never completed — the AI CLI call is still running or was interrupted
    mid-stream — is collapsed into a single partial-response record so the
    partial output stays visible on the timeline. A stream with no deltas has
    no partial output to show; its records are left untouched.

    Reasoning chunks form a separate stream with no terminal event of its own,
    so consecutive ``thinking`` records are merged into a single record instead.
    Reasoning is never mixed into the reply text.
    """
    dropped: set[int] = set()
    replaced: dict[int, dict[str, Any]] = {}
    pending: dict[str, list[int]] = {}
    reasoning: dict[str, list[int]] = {}
    for index, item in enumerate(records):
        if str(item.get("type") or "") != "agent_runtime.assistant":
            continue
        name = str(_payload(item).get("name") or "")
        stream = str(item.get("span_id") or "")
        if name in {"started", "delta"}:
            pending.setdefault(stream, []).append(index)
        elif name == "thinking":
            reasoning.setdefault(stream, []).append(index)
        elif name == "completed":
            dropped.update(pending.pop(stream, []))
    for indexes in reasoning.values():
        for run in _consecutive_runs(indexes):
            # A lone reasoning record is already what the timeline should show,
            # and separate runs must keep their own place on it.
            if len(run) < _MERGEABLE:
                continue
            anchor = run[-1]
            dropped.update(index for index in run if index != anchor)
            replaced[anchor] = {
                **records[anchor],
                "payload": {"name": "thinking", "message": _joined(records, run)},
            }
    for indexes in pending.values():
        deltas = [
            index
            for index in indexes
            if _payload(records[index]).get("name") == "delta"
        ]
        if not deltas:
            continue
        anchor = deltas[-1]
        dropped.update(index for index in indexes if index != anchor)
        replaced[anchor] = {
            **records[anchor],
            "payload": {
                "name": "partial",
                "message": _joined(records, deltas),
                "partial": True,
            },
        }
    return [
        replaced.get(index, item)
        for index, item in enumerate(records)
        if index not in dropped
    ]


def _consecutive_runs(indexes: list[int]) -> list[list[int]]:
    """Split ascending indexes into runs with nothing in between."""
    runs: list[list[int]] = []
    for index in indexes:
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


def _joined(records: list[dict[str, Any]], indexes: list[int]) -> str:
    return "".join(
        str(_payload(records[index]).get("message") or "") for index in indexes
    )[:MAX_MESSAGE]


def _payload(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("payload")
    return value if isinstance(value, dict) else {}
