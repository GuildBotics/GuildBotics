"""Unified persistence + query layer for runtime diagnostics records.

Events and logs published through :class:`~guildbotics.app_api.events.EventBus`
are recorded here under the unified schema (see
``docs/runtime_diagnostics_todo.ja.md``) so they can be aggregated by
``trace_id`` and survive an app restart. Prompt-trace records live in their own
JSONL file and are merged in by the query layer at read time.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from typing import Any

from guildbotics.utils.fileio import get_storage_path


def default_store_path() -> Path:
    return get_storage_path() / "run" / "diagnostics.jsonl"


class DiagnosticsStore:
    """In-memory + JSONL-backed store of unified diagnostics records."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        memory_limit: int = 5000,
        max_file_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self._path = path or default_store_path()
        self._memory_limit = memory_limit
        self._max_file_bytes = max_file_bytes
        self._records: deque[dict[str, Any]] = deque(maxlen=memory_limit)
        self._lock = threading.Lock()
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def record(self, item: dict[str, Any]) -> None:
        with self._lock:
            self._records.append(item)
            self._append_to_file(item)

    def list_traces(
        self,
        *,
        source: str | None = None,
        person_id: str | None = None,
        query: str | None = None,
        attr_key: str | None = None,
        attr_value: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._records)
        summaries: dict[str, dict[str, Any]] = {}
        for item in records:
            trace_id = item.get("trace_id")
            if not trace_id:
                continue
            summary = summaries.setdefault(trace_id, _new_summary(trace_id))
            _accumulate(summary, item)
        result = [
            _finalize_summary(summary)
            for summary in summaries.values()
            if _summary_matches(summary, source, person_id, query, attr_key, attr_value)
        ]
        # _summary_matches reads "_text" before _finalize_summary drops it.
        result.sort(key=lambda summary: summary["started_at"], reverse=True)
        return result[: max(1, limit)]

    def get_records(self, trace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            records = [
                item for item in self._records if item.get("trace_id") == trace_id
            ]
        records.sort(key=lambda item: item.get("timestamp", ""))
        return records

    def get_summary(self, trace_id: str) -> dict[str, Any] | None:
        summary = _new_summary(trace_id)
        found = False
        for item in self.get_records(trace_id):
            found = True
            _accumulate(summary, item)
        return _finalize_summary(summary) if found else None

    def global_records(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Return unscoped (``trace_id`` is empty) events and logs.

        These are records that do not belong to any execution unit — service
        lifecycle events (``scheduler.*`` / ``events.*``) and global/unscoped
        logs (app startup, diagnostics, background). Returned oldest-first
        (callers/UI reverse for display), capped to the most recent ``limit``.
        """
        with self._lock:
            records = [item for item in self._records if not item.get("trace_id")]
        records.sort(key=lambda item: item.get("timestamp", ""))
        return records[-max(1, limit) :]

    def delete_trace(self, trace_id: str) -> int:
        with self._lock:
            kept = [item for item in self._records if item.get("trace_id") != trace_id]
            removed = len(self._records) - len(kept)
            self._records = deque(kept, maxlen=self._memory_limit)
            self._rewrite_file()
        return removed

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open(encoding="utf-8") as handle:
                for line in handle:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        self._records.append(item)
        except OSError:
            return

    def _append_to_file(self, item: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if (
                self._path.exists()
                and self._path.stat().st_size >= self._max_file_bytes
            ):
                # ``item`` is already in ``self._records`` (appended by
                # ``record()`` before this call), so the rewrite persists it —
                # appending again would duplicate the row after a restart.
                self._rewrite_file()
                return
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        except OSError:
            return

    def _rewrite_file(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("w", encoding="utf-8") as handle:
                for item in self._records:
                    handle.write(
                        json.dumps(item, ensure_ascii=False, default=str) + "\n"
                    )
        except OSError:
            return


def _new_summary(trace_id: str) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "source": "",
        "person_id": "",
        "command": "",
        "workflow": "",
        "started_at": "",
        "updated_at": "",
        "status": "info",
        "event_count": 0,
        "log_count": 0,
        "error_count": 0,
        "span_count": 0,
        "attributes": {},
        "_spans": set(),
        "_text": [],
    }


def _accumulate(summary: dict[str, Any], item: dict[str, Any]) -> None:
    timestamp = str(item.get("timestamp", ""))
    if timestamp:
        if not summary["started_at"] or timestamp < summary["started_at"]:
            summary["started_at"] = timestamp
        if timestamp > summary["updated_at"]:
            summary["updated_at"] = timestamp
    for key in ("source", "person_id", "command", "workflow"):
        if not summary[key] and item.get(key):
            summary[key] = str(item.get(key))
    attributes = item.get("attributes")
    if isinstance(attributes, dict):
        for attr_key, attr_value in attributes.items():
            summary["attributes"].setdefault(attr_key, attr_value)
    span_id = item.get("span_id")
    if span_id:
        summary["_spans"].add(span_id)

    kind = item.get("kind")
    if kind == "event":
        summary["event_count"] += 1
        event_type = str(item.get("type", ""))
        if event_type.endswith(".failed"):
            summary["status"] = "failed"
            summary["error_count"] += 1
        elif event_type.endswith(".finished") and summary["status"] != "failed":
            summary["status"] = "success"
        elif event_type.endswith(".started") and summary["status"] == "info":
            summary["status"] = "running"
        summary["_text"].append(event_type)
    elif kind == "log":
        summary["log_count"] += 1
        level = str(item.get("level", "")).upper()
        if level in {"ERROR", "CRITICAL"}:
            summary["error_count"] += 1
        message = item.get("message")
        if isinstance(message, str):
            summary["_text"].append(message)


def _finalize_summary(summary: dict[str, Any]) -> dict[str, Any]:
    summary = dict(summary)
    summary["span_count"] = len(summary.pop("_spans"))
    summary.pop("_text", None)
    if not summary["started_at"]:
        summary["started_at"] = summary["updated_at"]
    return summary


def _summary_matches(
    summary: dict[str, Any],
    source: str | None,
    person_id: str | None,
    query: str | None,
    attr_key: str | None = None,
    attr_value: str | None = None,
) -> bool:
    if source and summary["source"] != source:
        return False
    if person_id and summary["person_id"] != person_id:
        return False
    # Exact match on a structured attribute (e.g. github.url / github.number /
    # slack.thread_ts) — the precise path for ticket/thread lookups.
    if (
        attr_key
        and attr_value
        and str(summary["attributes"].get(attr_key)) != attr_value
    ):
        return False
    if query:
        needle = query.lower()
        haystack = " ".join(
            [
                summary["trace_id"],
                summary["source"],
                summary["person_id"],
                summary["command"],
                summary["workflow"],
                json.dumps(summary["attributes"], ensure_ascii=False, default=str),
                " ".join(str(text) for text in summary.get("_text", [])),
            ]
        ).lower()
        if needle not in haystack:
            return False
    return True
