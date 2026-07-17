"""Small shared execution index and per-session transcript queries."""

from __future__ import annotations

import json
import os
import threading
import time
from base64 import b64decode, b64encode
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from guildbotics.observability.session_transcripts import (
    SYSTEM_TRACE_PREFIX,
    SessionTranscriptStore,
)
from guildbotics.utils.fileio import get_workspace_data_path
from guildbotics.utils.timestamps import parse_iso_datetime


def default_store_path() -> Path:
    return get_workspace_data_path("run", "diagnostics.jsonl")


_CURSOR_ANCHOR_BYTES = 256
_MIGRATION_LOCK_STALE_SECONDS = 10.0


@dataclass(frozen=True)
class DiagnosticsCursor:
    """Durable position in a diagnostics JSONL file."""

    offset: int
    device: int
    inode: int
    anchor: str

    def to_dict(self) -> dict[str, int | str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> DiagnosticsCursor | None:
        if not isinstance(value, dict):
            return None
        try:
            return cls(
                offset=max(0, int(value["offset"])),
                device=int(value["device"]),
                inode=int(value["inode"]),
                anchor=str(value["anchor"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


class DiagnosticsStore:
    """In-memory + JSONL-backed store of unified diagnostics records."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        memory_limit: int = 5000,
        max_file_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self._path_override = path
        self._path = path or default_store_path()
        self._migrated_path: Path | None = path
        self._memory_limit = memory_limit
        self._max_file_bytes = max_file_bytes
        self._records: deque[dict[str, Any]] = deque(maxlen=memory_limit)
        self._file_signature: tuple[int, int] | None = None
        self._lock = threading.Lock()
        self._transcripts = SessionTranscriptStore(self._path)
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None
        self._load_from_path(self._path)
        self._file_signature = self._file_signature_now()

    @property
    def path(self) -> Path:
        with self._lock:
            self._select_path_locked()
            return self._path

    def record(self, item: dict[str, Any]) -> None:
        with self._lock:
            self._refresh_path_locked()
            route = self._transcripts.route(item)
            for index_item in route.index_records:
                self._record_index_locked(index_item)

    def start_system_session(self, service_run_id: str = "") -> None:
        with self._lock:
            self._refresh_path_locked()
            route = self._transcripts.start_system_session(service_run_id)
            for item in route.index_records:
                self._record_index_locked(item)

    def finish_system_session(self) -> None:
        with self._lock:
            self._refresh_path_locked()
            route = self._transcripts.finish_system_session()
            for item in route.index_records:
                self._record_index_locked(item)

    def start_maintenance(self) -> None:
        with self._lock:
            self._refresh_path_locked()
            self._transcripts.prune_expired()
            if self._maintenance_thread is not None:
                return
            self._maintenance_stop.clear()
            self._maintenance_thread = threading.Thread(
                target=self._maintenance_loop,
                name="guildbotics-transcript-retention",
                daemon=True,
            )
            self._maintenance_thread.start()

    def stop_maintenance(self) -> None:
        self._maintenance_stop.set()
        thread = self._maintenance_thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._maintenance_thread = None

    def transcript_usage(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_path_locked()
            usage = self._transcripts.usage()
            usage["index_size_bytes"] = _file_size(self._path)
            return usage

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
            self._refresh_path_locked()
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
        if source is None and person_id is None and attr_key is None:
            result.extend(_system_summaries(records, query=query, include_latest=False))
        # _summary_matches reads "_text" before _finalize_summary drops it.
        result.sort(
            key=lambda summary: _timestamp_sort_key(summary["started_at"]), reverse=True
        )
        return result[: max(1, limit)]

    def get_records(self, trace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_path_locked()
            if trace_id.startswith(SYSTEM_TRACE_PREFIX):
                _, records = self._transcripts.system_records(
                    trace_id.removeprefix(SYSTEM_TRACE_PREFIX)
                )
            else:
                _, records = self._transcripts.trace_records(trace_id)
        records.sort(key=_record_timestamp_sort_key)
        return records

    def transcript_exists(self, trace_id: str) -> bool:
        with self._lock:
            self._refresh_path_locked()
            if trace_id.startswith(SYSTEM_TRACE_PREFIX):
                exists, _ = self._transcripts.system_records(
                    trace_id.removeprefix(SYSTEM_TRACE_PREFIX)
                )
            else:
                exists, _ = self._transcripts.trace_records(trace_id)
            return exists

    def get_summary(self, trace_id: str) -> dict[str, Any] | None:
        if trace_id.startswith(SYSTEM_TRACE_PREFIX):
            with self._lock:
                self._refresh_path_locked()
                summaries = _system_summaries(
                    list(self._records), query=None, include_latest=True
                )
            return next(
                (item for item in summaries if item["trace_id"] == trace_id), None
            )
        summary = _new_summary(trace_id)
        found = False
        with self._lock:
            self._refresh_path_locked()
            records = [
                item for item in self._records if item.get("trace_id") == trace_id
            ]
        for item in records:
            found = True
            _accumulate(summary, item)
        return _finalize_summary(summary) if found else None

    def global_records(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Return records from the most recent system session."""
        with self._lock:
            self._refresh_path_locked()
            session_id = self._transcripts.latest_system_session_id(list(self._records))
            if session_id is None:
                return []
            _, records = self._transcripts.system_records(session_id)
        records.sort(key=_record_timestamp_sort_key)
        return records[-max(1, limit) :]

    def latest_system_trace_id(self) -> str | None:
        with self._lock:
            self._refresh_path_locked()
            session_id = self._transcripts.latest_system_session_id(list(self._records))
        return f"{SYSTEM_TRACE_PREFIX}{session_id}" if session_id else None

    def records_between(
        self,
        *,
        includes: Callable[[str], bool],
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return records whose timestamp satisfies ``includes``.

        The caller owns timestamp parsing so this store remains a generic JSONL
        persistence layer. Returned records are oldest-first and capped to the
        most recent ``limit`` matching rows.
        """
        with self._lock:
            self._refresh_path_locked()
            records = [
                item
                for item in self._records
                if includes(str(item.get("timestamp", "")))
                and item.get("type")
                not in {"session.pointer", "system.started", "system.finished"}
            ]
        records.sort(key=_record_timestamp_sort_key)
        return records[-max(1, limit) :]

    def records_after(
        self,
        cursor: DiagnosticsCursor | None,
        *,
        includes: Callable[[dict[str, Any]], bool],
    ) -> tuple[list[dict[str, Any]], DiagnosticsCursor]:
        """Return matching complete rows appended after ``cursor``.

        The cursor advances across every row, including malformed and filtered
        rows, so consumers can process an append-only JSONL stream without
        rescanning the in-memory diagnostics window. File replacement,
        truncation, and rewrite-based rotation reset the cursor safely.
        """
        with self._lock:
            self._refresh_path_locked()
            return self._records_after_locked(cursor, includes=includes)

    def current_cursor(self) -> DiagnosticsCursor:
        """Return a cursor after the last complete row currently on disk."""
        with self._lock:
            self._refresh_path_locked()
            _, cursor = self._records_after_locked(None, includes=lambda _: False)
            return cursor

    # -- persistence ---------------------------------------------------------

    def _refresh_path_locked(self) -> None:
        path_changed = self._select_path_locked()
        migration_completed = self._ensure_migrated_locked()
        if path_changed or migration_completed:
            return
        # Same file, but another process (e.g. a ``guildbotics member`` CLI
        # subprocess) may have appended records to it since we last read. A
        # long-lived reader such as the app_api backend would otherwise serve a
        # stale in-memory snapshot and miss those rows.
        self._reload_if_changed_locked()

    def _select_path_locked(self) -> bool:
        if self._path_override is None:
            path = default_store_path()
            if path != self._path:
                self._path = path
                self._migrated_path = None
                self._transcripts = SessionTranscriptStore(self._path)
                self._reload_from_path_locked()
                return True
        return False

    def _ensure_migrated_locked(self) -> bool:
        if self._path_override is not None or self._migrated_path == self._path:
            return False
        if not _migrate_legacy_store(self._path):
            return False
        self._migrated_path = self._path
        self._reload_from_path_locked()
        return True

    def _records_after_locked(
        self,
        cursor: DiagnosticsCursor | None,
        *,
        includes: Callable[[dict[str, Any]], bool],
    ) -> tuple[list[dict[str, Any]], DiagnosticsCursor]:
        try:
            with self._path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                offset = self._validated_offset(handle, stat, cursor)
                handle.seek(offset)
                records: list[dict[str, Any]] = []
                while line := handle.readline():
                    if not line.endswith(b"\n"):
                        break
                    offset = handle.tell()
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if isinstance(item, dict) and includes(item):
                        records.append(item)
                return records, self._cursor_at(handle, stat, offset)
        except OSError:
            return [], DiagnosticsCursor(offset=0, device=0, inode=0, anchor="")

    @staticmethod
    def _validated_offset(
        handle: Any, stat: Any, cursor: DiagnosticsCursor | None
    ) -> int:
        if cursor is None:
            return 0
        try:
            anchor = b64decode(cursor.anchor, validate=True)
        except ValueError:
            return 0
        if not anchor:
            return 0
        if (
            cursor.device == stat.st_dev
            and cursor.inode == stat.st_ino
            and cursor.offset <= stat.st_size
        ):
            anchor_start = cursor.offset - len(anchor)
            if anchor_start >= 0:
                handle.seek(anchor_start)
                if handle.read(len(anchor)) == anchor:
                    return cursor.offset
        # Rewrite-based rotation keeps the newest diagnostics rows. Relocate
        # the small tail anchor so retained rows are not replayed.
        handle.seek(0)
        position = handle.read().rfind(anchor)
        return position + len(anchor) if position >= 0 else 0

    @staticmethod
    def _cursor_at(handle: Any, stat: Any, offset: int) -> DiagnosticsCursor:
        anchor_start = max(0, offset - _CURSOR_ANCHOR_BYTES)
        handle.seek(anchor_start)
        anchor = handle.read(offset - anchor_start)
        # Prefer an anchor wholly contained in the final JSONL row. Rotation
        # can discard the preceding pointer while retaining the terminal event;
        # including bytes from both rows would make that retained event look new.
        if anchor.endswith(b"\n"):
            final_row_start = anchor.rfind(b"\n", 0, len(anchor) - 1) + 1
            anchor = anchor[final_row_start:]
        return DiagnosticsCursor(
            offset=offset,
            device=stat.st_dev,
            inode=stat.st_ino,
            anchor=b64encode(anchor).decode("ascii"),
        )

    def _reload_if_changed_locked(self) -> None:
        if self._file_signature_now() != self._file_signature:
            self._reload_from_path_locked()

    def _reload_from_path_locked(self) -> None:
        self._records.clear()
        self._load_from_path(self._path)
        self._file_signature = self._file_signature_now()

    def _file_signature_now(self) -> tuple[int, int] | None:
        try:
            stat = self._path.stat()
        except OSError:
            return None
        return (stat.st_size, stat.st_mtime_ns)

    def _load_from_path(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            with path.open(encoding="utf-8") as handle:
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

    def _record_index_locked(self, item: dict[str, Any]) -> None:
        self._records.append(item)
        self._append_to_file(item)
        # Our own append changed the file. Capture the new signature so this
        # process does not reload the whole file on its next read, while
        # appends from other processes still trigger a reload.
        self._file_signature = self._file_signature_now()

    def _maintenance_loop(self) -> None:
        while not self._maintenance_stop.wait(24 * 60 * 60):
            with self._lock:
                self._refresh_path_locked()
                self._transcripts.prune_expired()

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
        "_started_at_key": None,
        "_updated_at_key": None,
    }


def _accumulate(summary: dict[str, Any], item: dict[str, Any]) -> None:
    timestamp = str(item.get("timestamp", ""))
    if timestamp:
        timestamp_key = _timestamp_sort_key(timestamp)
        if (
            summary["_started_at_key"] is None
            or timestamp_key < summary["_started_at_key"]
        ):
            summary["started_at"] = timestamp
            summary["_started_at_key"] = timestamp_key
        if (
            summary["_updated_at_key"] is None
            or timestamp_key > summary["_updated_at_key"]
        ):
            summary["updated_at"] = timestamp
            summary["_updated_at_key"] = timestamp_key
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
        if event_type.endswith((".failed", ".error")):
            summary["status"] = "failed"
            summary["error_count"] += 1
        elif event_type.endswith(".finished") and summary["status"] != "failed":
            summary["status"] = "success"
        elif event_type.endswith(".started") and summary["status"] == "info":
            summary["status"] = "running"
        summary["_text"].append(event_type)
        payload = item.get("payload")
        if event_type.endswith((".finished", ".failed")) and isinstance(payload, dict):
            for key in ("event_count", "log_count", "error_count", "span_count"):
                value = payload.get(key)
                if isinstance(value, int):
                    summary[key] = value
    elif kind == "log":
        summary["log_count"] += 1
        level = str(item.get("level", "")).upper()
        if level in {"ERROR", "CRITICAL"}:
            summary["error_count"] += 1
        message = item.get("message")
        if isinstance(message, str):
            summary["_text"].append(message)
    elif kind == "memory":
        summary["_text"].append(str(item.get("type", "")))
        payload = item.get("payload")
        if isinstance(payload, dict):
            summary["_text"].append(
                json.dumps(payload, ensure_ascii=False, default=str)
            )


def _finalize_summary(summary: dict[str, Any]) -> dict[str, Any]:
    summary = dict(summary)
    summary["span_count"] = max(summary["span_count"], len(summary.pop("_spans")))
    summary.pop("_text", None)
    summary.pop("_started_at_key", None)
    summary.pop("_updated_at_key", None)
    if not summary["started_at"]:
        summary["started_at"] = summary["updated_at"]
    return summary


def _record_timestamp_sort_key(item: dict[str, Any]) -> tuple[float, str]:
    return _timestamp_sort_key(str(item.get("timestamp", "")))


def _timestamp_sort_key(timestamp: str) -> tuple[float, str]:
    parsed = parse_iso_datetime(timestamp)
    if parsed is None:
        return (0.0, timestamp)
    return (parsed.timestamp(), timestamp)


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


def _system_summaries(
    records: list[dict[str, Any]], *, query: str | None, include_latest: bool
) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    finished: set[str] = set()
    for item in records:
        if item.get("type") not in {"system.started", "system.finished"}:
            continue
        attributes = item.get("attributes")
        if not isinstance(attributes, dict):
            continue
        session_id = str(attributes.get("system_session_id") or "")
        if not session_id:
            continue
        if session_id not in summaries:
            order.append(session_id)
            summaries[session_id] = _new_summary(f"{SYSTEM_TRACE_PREFIX}{session_id}")
            summaries[session_id]["source"] = "system"
            summaries[session_id]["command"] = "Global / system"
        _accumulate(summaries[session_id], item)
        if item.get("type") == "system.finished":
            finished.add(session_id)
    latest = order[-1] if order else ""
    result: list[dict[str, Any]] = []
    for session_id, summary in summaries.items():
        if session_id == latest and not include_latest:
            continue
        if session_id not in finished and session_id != latest:
            summary["status"] = "interrupted"
        if query and not _summary_matches(summary, None, None, query, None, None):
            continue
        result.append(_finalize_summary(summary))
    return result


def _migrate_legacy_store(path: Path) -> bool:
    """Discard the pre-transcript index once per workspace data root."""
    marker = path.parent / ".session-transcripts-v1"
    if marker.exists():
        return True
    lock_path = path.parent / ".session-transcripts-v1.lock"
    descriptor: int | None = None
    for _ in range(2):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, f"{os.getpid()}\n".encode())
            break
        except FileExistsError:
            if not _migration_lock_is_stale(lock_path):
                for _ in range(100):
                    if marker.exists():
                        return True
                    if _migration_lock_is_stale(lock_path):
                        break
                    time.sleep(0.01)
                if not _migration_lock_is_stale(lock_path):
                    return False
            with suppress(OSError):
                lock_path.unlink()
        except OSError:
            return False
    if descriptor is None:
        return marker.exists()
    try:
        os.close(descriptor)
        try:
            path.with_name("prompt_trace.jsonl").unlink(missing_ok=True)
            path.write_text("", encoding="utf-8")
            marker.write_text("1\n", encoding="utf-8")
        except OSError:
            return False
    finally:
        with suppress(OSError):
            lock_path.unlink(missing_ok=True)
    return True


def _migration_lock_is_stale(path: Path) -> bool:
    try:
        raw_pid = path.read_text(encoding="utf-8").strip()
        modified_at = path.stat().st_mtime
    except OSError:
        return True
    if time.time() - modified_at >= _MIGRATION_LOCK_STALE_SECONDS:
        return True
    try:
        pid = int(raw_pid)
    except ValueError:
        pid = 0
    if pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            return True
        return False
    return False


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
