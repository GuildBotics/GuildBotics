"""Shared provider-neutral activity events stored as one file per event."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from guildbotics.observability.event_types import (
    COMMAND_LIFECYCLE_EVENT_TYPES,
    SYNC_UPDATE_REJECTED,
)
from guildbotics.utils.fileio import get_workspace_state_path
from guildbotics.utils.shared_redaction import (
    MAX_SHARED_TEXT_CHARS,
    redact_for_sharing,
)
from guildbotics.utils.workspace_sync_port import (
    SHARED_RECORD_SCHEMA_VERSION,
    write_shared_json,
)

_MAX_SAFE_SUMMARY_CHARS = MAX_SHARED_TEXT_CHARS
# Explicit allowlist of what shared activity history carries (workspace sync
# plan §8.1): provider domain outcomes, workflow / command start, completion,
# and failure, and retry / abandonment decisions. Device-health events
# (credential probes, diagnostics and verify runs, scheduler worker failures)
# stay in local diagnostics; a new event type must opt in here.
_DOMAIN_EVENT_TYPES = COMMAND_LIFECYCLE_EVENT_TYPES | frozenset(
    {
        "github.push",
        "github.pull_request",
        "github.issue",
        "github.issue_comment",
        "workflow.completed",
        "workflow.completion_missing",
        "workflow.rate_limited",
        "chat_dispatch.retry_scheduled",
        "chat_dispatch.abandoned",
        SYNC_UPDATE_REJECTED,
    }
)


def is_domain_activity_event(event_type: str) -> bool:
    """True when the event belongs in shared activity history."""
    return event_type in _DOMAIN_EVENT_TYPES


def default_events_root() -> Path:
    return get_workspace_state_path("events")


class ActivityEventStore:
    """Write and list shared activity events as one JSON file per event.

    Args:
        root (Path | None): The events directory, or None for the selected
            workspace's.
        workspace_root (Path | None): The workspace the events belong to. A
            caller that already knows which workspace it is acting for passes
            it, so recording does not resolve the selected workspace a second
            time and land in a different one while workspaces are switching.
    """

    def __init__(
        self, root: Path | None = None, *, workspace_root: Path | None = None
    ) -> None:
        self.root = root if root is not None else default_events_root()
        self.workspace_root = workspace_root

    def record(self, record: dict[str, Any]) -> Path:
        event = _to_activity_event(record)
        occurred = str(event["occurred_at"])
        year, month = _year_month(occurred)
        path = self.root / year / month / f"{event['event_id']}.json"
        write_shared_json(path, event, workspace_root=self.workspace_root)
        return path

    def list_between(
        self,
        start: datetime,
        end: datetime,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if not self.root.exists():
            return events
        for path in sorted(self.root.glob("*/*/*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            occurred = _parse_occurred(payload.get("occurred_at"))
            if occurred is None or occurred < start or occurred > end:
                continue
            events.append(payload)
        events.sort(
            key=lambda item: (str(item.get("occurred_at")), str(item.get("event_id")))
        )
        if limit is not None:
            events = events[-limit:]
        return events

    def records_between(
        self,
        start: datetime,
        end: datetime,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return events in the diagnostics-record shape used by Activity history."""
        return [
            _as_diagnostics_record(item)
            for item in self.list_between(start, end, limit=limit)
        ]


def _to_activity_event(record: dict[str, Any]) -> dict[str, Any]:
    event_id = str(record.get("event_id") or uuid.uuid4().hex)
    occurred_at = str(
        record.get("timestamp") or record.get("occurred_at") or _utc_now()
    )
    summary = str(record.get("safe_summary") or record.get("type") or "")
    if len(summary) > _MAX_SAFE_SUMMARY_CHARS:
        summary = summary[:_MAX_SAFE_SUMMARY_CHARS]
    payload = record.get("payload")
    attributes = record.get("attributes")
    event = {
        "schema_version": SHARED_RECORD_SCHEMA_VERSION,
        "event_id": event_id,
        "workspace_id": str(record.get("workspace_id") or ""),
        "occurred_at": occurred_at,
        "device_id": str(record.get("device_id") or ""),
        "member_id": str(record.get("person_id") or record.get("member_id") or ""),
        "kind": str(record.get("type") or record.get("kind") or "event"),
        "subject": str(record.get("subject") or ""),
        "safe_summary": summary,
        "links": list(record.get("links") or []),
        "run_id": str(record.get("run_id") or ""),
        "local_trace_id": str(
            record.get("trace_id") or record.get("local_trace_id") or ""
        ),
        "payload": payload if isinstance(payload, dict) else {},
        "attributes": attributes if isinstance(attributes, dict) else {},
        "source": str(record.get("source") or ""),
        "command": str(record.get("command") or ""),
        "workflow": str(record.get("workflow") or ""),
    }
    # One uniform pass over the whole event enforces both shared-boundary
    # guarantees for every current and future field.
    result: dict[str, Any] = redact_for_sharing(event)
    return result


def _as_diagnostics_record(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "event",
        "type": event.get("kind") or "event",
        "timestamp": event.get("occurred_at"),
        "trace_id": event.get("local_trace_id") or None,
        "person_id": event.get("member_id") or "",
        "source": event.get("source") or "",
        "command": event.get("command") or "",
        "workflow": event.get("workflow") or "",
        "payload": event.get("payload") or {},
        "attributes": event.get("attributes") or {},
        "event_id": event.get("event_id"),
        "local_trace_id": event.get("local_trace_id") or "",
        "device_id": event.get("device_id") or "",
    }


def _year_month(occurred_at: str) -> tuple[str, str]:
    parsed = _parse_occurred(occurred_at)
    if parsed is None:
        now = datetime.now(UTC)
        return f"{now.year:04d}", f"{now.month:02d}"
    return f"{parsed.year:04d}", f"{parsed.month:02d}"


def _parse_occurred(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
