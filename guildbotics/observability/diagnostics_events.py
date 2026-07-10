from __future__ import annotations

from datetime import datetime
from typing import Any

from guildbotics.observability import correlation_fields
from guildbotics.observability.diagnostics_store import DiagnosticsStore


def record_correlated_event(
    *,
    event_type: str,
    payload: dict[str, Any],
    attributes: dict[str, Any] | None = None,
    default_source: str = "",
    person_id: str = "",
    command: str | None = None,
    timestamp: str | None = None,
) -> None:
    correlation = correlation_fields()
    merged_attributes = dict(correlation.get("attributes") or {})
    merged_attributes.update(
        {key: value for key, value in (attributes or {}).items() if value}
    )
    DiagnosticsStore().record(
        {
            "kind": "event",
            "type": event_type,
            "trace_id": correlation.get("trace_id"),
            "span_id": correlation.get("span_id"),
            "parent_id": correlation.get("parent_id"),
            "call_id": correlation.get("call_id"),
            "span": correlation.get("span", ""),
            "source": correlation.get("source") or default_source,
            "person_id": person_id or str(correlation.get("person_id") or ""),
            "command": command
            if command is not None
            else str(correlation.get("command") or ""),
            "workflow": correlation.get("workflow", ""),
            "attributes": merged_attributes,
            "payload": payload,
            "timestamp": timestamp or datetime.now().astimezone().isoformat(),
        }
    )
