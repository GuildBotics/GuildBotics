from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from guildbotics.capabilities.task_runs import RUN_ENV, TASK_RUN_ENV
from guildbotics.observability import correlation_fields
from guildbotics.utils.fileio import get_workspace_data_path
from guildbotics.utils.timestamps import parse_iso_datetime

MEMORY_AUDIT_FILE = "memory_events.jsonl"
DEFAULT_MEMORY_AUDIT_LIMIT = 5000
DEFAULT_MEMORY_AUDIT_MAX_BYTES = 8 * 1024 * 1024
_MEMORY_AUDIT_LOCK = threading.Lock()


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
    query_keywords: list[str] | None = None,
    result_count: int | None = None,
    duration_ms: float | None = None,
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
    if result_count is not None:
        attributes["memory.result_count"] = result_count
    if duration_ms is not None:
        attributes["memory.duration_ms"] = duration_ms

    payload: dict[str, Any] = {
        "title": title,
        "summary": summary,
        "source": source_entries,
        "changed_fields": changed_fields or [],
    }
    if query_keywords is not None:
        payload["query_keywords"] = query_keywords
    if result_count is not None:
        payload["result_count"] = result_count
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms

    if action == "recall":
        query_label = ", ".join(query_keywords or []) or "all documents"
        hit_label = "hit" if result_count == 1 else "hits"
        message = (
            f"memory recall: {query_label} ({result_count or 0} {hit_label}"
            f" in {duration_ms or 0:.2f}ms)"
        )
    else:
        message = f"memory {action}: {title or doc_id}"

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
        "message": message,
        "attributes": attributes,
        "payload": payload,
    }
    MemoryAuditStore().record(item)


class MemoryAuditStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        max_file_bytes: int = DEFAULT_MEMORY_AUDIT_MAX_BYTES,
    ) -> None:
        self._path = path
        self._max_file_bytes = max_file_bytes

    @property
    def path(self) -> Path:
        return self._path or default_memory_audit_path()

    def record(self, item: dict[str, Any]) -> None:
        path = self.path
        line = self._bounded_line(item)
        if not line:
            return
        try:
            with _MEMORY_AUDIT_LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                current_size = path.stat().st_size if path.exists() else 0
                if current_size + len(line.encode("utf-8")) + 1 > self._max_file_bytes:
                    self._rewrite_with_newest(path, line)
                    return
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
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
        matches.sort(key=_timestamp_sort_key, reverse=True)
        return matches[: max(1, limit)]

    def _read_events(self) -> list[dict[str, Any]]:
        path = self.path
        if not path.is_file():
            return []
        try:
            with _MEMORY_AUDIT_LOCK:
                lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [
            item
            for item in (_json_object(line.strip()) for line in lines)
            if item is not None
        ]

    def _rewrite_with_newest(self, path: Path, newest: str) -> None:
        try:
            existing = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            existing = []
        retained = [newest]
        size = len(newest.encode("utf-8")) + 1
        for line in reversed(existing):
            line_size = len(line.encode("utf-8")) + 1
            if size + line_size > self._max_file_bytes:
                break
            retained.append(line)
            size += line_size
        try:
            path.write_text("\n".join(reversed(retained)) + "\n", encoding="utf-8")
        except OSError:
            return

    def _bounded_line(self, item: dict[str, Any]) -> str:
        line = json.dumps(item, ensure_ascii=False, default=str)
        size = len(line.encode("utf-8")) + 1
        if size <= self._max_file_bytes:
            return line
        compact = {
            key: value
            for key, value in item.items()
            if key not in {"attributes", "payload", "message"}
        }
        compact["message"] = (
            "memory audit payload omitted because it exceeded the file limit"
        )
        compact["attributes"] = {
            key: value
            for key, value in _dict(item.get("attributes")).items()
            if key in {"memory.action", "memory.doc_id", "memory.scope", "memory.kind"}
        }
        compact["payload"] = {
            "truncated": True,
            "original_size_bytes": size,
        }
        line = json.dumps(compact, ensure_ascii=False, default=str)
        if len(line.encode("utf-8")) + 1 <= self._max_file_bytes:
            return line
        fallback = json.dumps(
            {"truncated": True, "original_size_bytes": size},
            ensure_ascii=False,
        )
        if len(fallback.encode("utf-8")) + 1 <= self._max_file_bytes:
            return fallback
        return ""


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
    timestamp_value = parse_memory_audit_timestamp(timestamp)
    if person_id and item.get("person_id") != person_id:
        return False
    if doc_id and attributes.get("memory.doc_id") != doc_id:
        return False
    if action and attributes.get("memory.action") != action:
        return False
    if trace_id and item.get("trace_id") != trace_id:
        return False
    since_value = parse_memory_audit_timestamp(since)
    if since_value is not None and (
        timestamp_value is None or timestamp_value < since_value
    ):
        return False
    until_value = parse_memory_audit_timestamp(until)
    if until_value is not None and (
        timestamp_value is None or timestamp_value > until_value
    ):
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


def _timestamp_sort_key(item: dict[str, Any]) -> datetime:
    return parse_memory_audit_timestamp(
        str(item.get("timestamp") or "")
    ) or datetime.min.replace(tzinfo=UTC)


def parse_memory_audit_timestamp(value: str | None) -> datetime | None:
    parsed = parse_iso_datetime(value)
    return parsed.astimezone(UTC) if parsed is not None else None


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
