from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from guildbotics.capabilities.task_runs import RUN_ENV, TASK_RUN_ENV
from guildbotics.observability import correlation_fields
from guildbotics.utils.fileio import get_workspace_data_path

MEMORY_AUDIT_FILE = "memory_events.jsonl"
DEFAULT_MEMORY_AUDIT_LIMIT = 5000


def default_memory_audit_path() -> Path:
    return get_workspace_data_path("documents", MEMORY_AUDIT_FILE)


def append_memory_event(
    *,
    action: str,
    person_id: str,
    scope: str,
    doc_id: str,
    path: str,
    title: str,
    summary: str,
    kind: str,
    source_entries: list[dict[str, Any]],
    changed_fields: list[str] | None = None,
) -> None:
    correlation = correlation_fields()
    attributes = _dict(correlation.get("attributes"))
    run_id = os.getenv(RUN_ENV, "")
    task_run_id = os.getenv(TASK_RUN_ENV, "")
    attributes.update(
        {
            "memory.action": action,
            "memory.doc_id": doc_id,
            "memory.scope": scope,
            "memory.path": path,
            "memory.kind": kind,
        }
    )
    if run_id:
        attributes["run_id"] = run_id
    if task_run_id:
        attributes["task_run_id"] = task_run_id

    item = {
        "kind": "memory",
        "type": f"memory.{action}",
        "timestamp": _now(),
        "trace_id": correlation.get("trace_id"),
        "span_id": correlation.get("span_id"),
        "parent_id": correlation.get("parent_id"),
        "call_id": correlation.get("call_id"),
        "span": str(correlation.get("span") or ""),
        "source": str(correlation.get("source") or ""),
        "person_id": person_id,
        "command": str(correlation.get("command") or ""),
        "workflow": str(correlation.get("workflow") or ""),
        "message": f"memory {action}: {title or doc_id}",
        "attributes": attributes,
        "payload": {
            "title": title,
            "summary": summary,
            "source": source_entries,
            "changed_fields": changed_fields or [],
        },
    }
    MemoryAuditStore().record(item)


class MemoryAuditStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path or default_memory_audit_path()

    def record(self, item: dict[str, Any]) -> None:
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        except OSError:
            return

    def list_events(
        self,
        *,
        person_id: str | None = None,
        doc_id: str | None = None,
        action: str | None = None,
        source: str | None = None,
        query: str | None = None,
        since: str | None = None,
        until: str | None = None,
        trace_id: str | None = None,
        limit: int = DEFAULT_MEMORY_AUDIT_LIMIT,
    ) -> list[dict[str, Any]]:
        matches = [
            item
            for item in self._read_events()
            if _matches_event(
                item,
                person_id=person_id,
                doc_id=doc_id,
                action=action,
                source=source,
                query=query,
                since=since,
                until=until,
                trace_id=trace_id,
            )
        ]
        matches.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
        return matches[: max(1, limit)]

    def _read_events(self) -> list[dict[str, Any]]:
        path = self.path
        if not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [
            item
            for item in (_json_object(line.strip()) for line in lines)
            if item is not None
        ]


def _matches_event(
    item: dict[str, Any],
    *,
    person_id: str | None,
    doc_id: str | None,
    action: str | None,
    source: str | None,
    query: str | None,
    since: str | None,
    until: str | None,
    trace_id: str | None,
) -> bool:
    attributes = _dict(item.get("attributes"))
    payload = _dict(item.get("payload"))
    timestamp = str(item.get("timestamp") or "")
    if person_id and item.get("person_id") != person_id:
        return False
    if doc_id and attributes.get("memory.doc_id") != doc_id:
        return False
    if action and attributes.get("memory.action") != action:
        return False
    if trace_id and item.get("trace_id") != trace_id:
        return False
    if since and timestamp < since:
        return False
    if until and timestamp > until:
        return False
    if (
        source
        and source.lower()
        not in json.dumps(
            payload.get("source", []), ensure_ascii=False, default=str
        ).lower()
    ):
        return False
    if query:
        needle = query.lower()
        haystack = json.dumps(
            {
                "type": item.get("type"),
                "message": item.get("message"),
                "person_id": item.get("person_id"),
                "attributes": attributes,
                "payload": payload,
            },
            ensure_ascii=False,
            default=str,
        ).lower()
        if needle not in haystack:
            return False
    return True


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json_object(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        item = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return item if isinstance(item, dict) else None


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat()
