"""Shared provider-neutral activity events stored as one file per event."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from guildbotics.observability.event_types import COMMAND_LIFECYCLE_EVENT_TYPES
from guildbotics.utils.fileio import (
    get_workspace_config_dir,
    get_workspace_state_path,
)

ACTIVITY_EVENT_SCHEMA_VERSION = 1
_MAX_SAFE_SUMMARY_CHARS = 500
_MAX_SHARED_TEXT_CHARS = 500
# Values shorter than this are too collision-prone to mask ("1", "true", ...).
_MIN_MASKED_LENGTH = 8
# The shared store is synchronized between the single user's own machines, so
# the boundary protects exactly two things (and deliberately nothing more):
# secret values must not enter the durable synced history, and the history
# must stay small. Both are enforced uniformly in ``_shared_value`` (secret
# masking + size bounds) instead of per-field rules, so new fields and new
# requirements need no bookkeeping. Full console / prompt bodies stay local
# because they are bulk log data (§8.1), not because of their field names.
_LOCAL_ONLY_PAYLOAD_KEYS = frozenset(
    {"stdout", "stderr", "prompt", "response", "messages"}
)
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
    }
)


def is_domain_activity_event(event_type: str) -> bool:
    """True when the event belongs in shared activity history."""
    return event_type in _DOMAIN_EVENT_TYPES


def default_events_root() -> Path:
    return get_workspace_state_path("events")


class ActivityEventStore:
    """Write and list shared activity events as one JSON file per event."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else default_events_root()

    def record(self, record: dict[str, Any]) -> Path:
        event = _to_activity_event(record)
        occurred = str(event["occurred_at"])
        year, month = _year_month(occurred)
        path = self.root / year / month / f"{event['event_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
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
        "schema_version": ACTIVITY_EVENT_SCHEMA_VERSION,
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
    result: dict[str, Any] = _shared_value(event, _workspace_secret_values())
    return result


def _shared_value(value: Any, secret_values: tuple[str, ...]) -> Any:
    """Apply the two shared-boundary guarantees to a payload value.

    Every string is masked against the workspace's known secret values and
    truncated to a bound. Working value-first (not field-name-first) means a
    new field or event cannot leak a secret or bloat the synced history no
    matter what shape it arrives in. Bulk log bodies (stdout/prompt/...) are
    additionally dropped by key since they have no cross-device value.
    """
    if isinstance(value, dict):
        return {
            str(key): _shared_value(item, secret_values)
            for key, item in value.items()
            if str(key) not in _LOCAL_ONLY_PAYLOAD_KEYS
        }
    if isinstance(value, list):
        return [_shared_value(item, secret_values) for item in value]
    if isinstance(value, str):
        masked = value
        for secret in secret_values:
            if secret in masked:
                masked = masked.replace(secret, "***")
        return masked[:_MAX_SHARED_TEXT_CHARS]
    return value


def _workspace_secret_values() -> tuple[str, ...]:
    """Secret values currently visible to this process, for masking.

    Read from ``os.environ`` for the keys named in the workspace secrets
    index — the realistic way a secret ends up inside an error message —
    so recording an event never has to open the OS keychain.
    """
    try:
        from guildbotics.utils.secret_store import KeyringSecretStore

        keys = KeyringSecretStore(get_workspace_config_dir()).keys()
    except Exception:
        return ()
    values = {
        value
        for key in keys
        if (value := os.environ.get(key, "")) and len(value) >= _MIN_MASKED_LENGTH
    }
    return tuple(sorted(values, key=len, reverse=True))


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
