"""Per-execution transcript persistence for runtime diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_TRANSCRIPT_DETAIL = "standard"
DEFAULT_TRANSCRIPT_RETENTION_DAYS = 30
TRANSCRIPT_DETAIL_ENV = "GUILDBOTICS_TRANSCRIPT_DETAIL"
TRANSCRIPT_RETENTION_DAYS_ENV = "GUILDBOTICS_TRANSCRIPT_RETENTION_DAYS"
STANDARD_STDERR_TAIL_BYTES = 8 * 1024
SYSTEM_TRACE_PREFIX = "system:"
MAX_SESSION_ID_LENGTH = 180

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
_WRITE_LOCK = threading.Lock()
_SYSTEM_LOCK = threading.Lock()
_SYSTEM_SESSIONS: dict[Path, SystemSession] = {}


@dataclass
class TranscriptCounts:
    event_count: int = 0
    log_count: int = 0
    error_count: int = 0
    span_count: int = 0
    spans: set[str] = field(default_factory=set)

    def add(self, item: dict[str, Any]) -> None:
        kind = str(item.get("kind") or "")
        if kind == "event":
            self.event_count += 1
            if str(item.get("type") or "").endswith((".failed", ".error")):
                self.error_count += 1
        elif kind == "log":
            self.log_count += 1
            if str(item.get("level") or "").upper() in {"ERROR", "CRITICAL"}:
                self.error_count += 1
        span_id = str(item.get("span_id") or "")
        if span_id:
            self.spans.add(span_id)
            self.span_count = len(self.spans)

    def payload(self) -> dict[str, int]:
        return {
            "event_count": self.event_count,
            "log_count": self.log_count,
            "error_count": self.error_count,
            "span_count": self.span_count,
        }


@dataclass(frozen=True)
class SystemSession:
    session_id: str
    path: Path
    started_at: str
    service_run_id: str


@dataclass(frozen=True)
class TranscriptRoute:
    index_records: list[dict[str, Any]]


def transcript_detail() -> str:
    value = os.getenv(TRANSCRIPT_DETAIL_ENV, DEFAULT_TRANSCRIPT_DETAIL)
    return "full" if value.strip().lower() == "full" else DEFAULT_TRANSCRIPT_DETAIL


def transcript_retention_days() -> int:
    raw = os.getenv(
        TRANSCRIPT_RETENTION_DAYS_ENV, str(DEFAULT_TRANSCRIPT_RETENTION_DAYS)
    )
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_TRANSCRIPT_RETENTION_DAYS


def standard_stderr_tail(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= STANDARD_STDERR_TAIL_BYTES:
        return value
    return encoded[-STANDARD_STDERR_TAIL_BYTES:].decode("utf-8", errors="replace")


def should_record_agent_event(kind: str, name: str) -> bool:
    if transcript_detail() == "full":
        return True
    lowered = f"{kind} {name}".lower()
    return not any(part in lowered for part in ("delta", "thinking", "reasoning"))


class SessionTranscriptStore:
    """Write and read JSONL transcripts next to a diagnostics index."""

    def __init__(self, index_path: Path) -> None:
        self.index_path = index_path
        self.sessions_dir = index_path.parent / "sessions"

    def route(self, item: dict[str, Any]) -> TranscriptRoute:
        trace_id = str(item.get("trace_id") or "")
        if trace_id:
            return self._route_trace(trace_id, item)
        return self._route_system(item)

    def start_system_session(self, service_run_id: str = "") -> TranscriptRoute:
        session, created = self._ensure_system_session(service_run_id)
        if not created:
            return TranscriptRoute(index_records=[])
        item = self._system_boundary_record("system.started", session)
        self._append(session.path, item)
        return TranscriptRoute(index_records=[item])

    def finish_system_session(self) -> TranscriptRoute:
        root = self.sessions_dir.resolve()
        with _SYSTEM_LOCK:
            session = _SYSTEM_SESSIONS.pop(root, None)
        if session is None:
            return TranscriptRoute(index_records=[])
        item = self._system_boundary_record("system.finished", session)
        item = self._with_final_counts(item, session.path, include=item)
        self._append(session.path, item)
        return TranscriptRoute(index_records=[item])

    def trace_records(self, trace_id: str) -> tuple[bool, list[dict[str, Any]]]:
        path = self.trace_path(trace_id)
        return path.is_file(), self._read(path)

    def system_records(self, session_id: str) -> tuple[bool, list[dict[str, Any]]]:
        path = self.sessions_dir / f"{_safe_id(session_id)}.jsonl"
        return path.is_file(), self._read(path)

    def latest_system_session_id(
        self, index_records: list[dict[str, Any]]
    ) -> str | None:
        sessions = [
            str(_attributes(item).get("system_session_id") or "")
            for item in index_records
            if item.get("type") == "system.started"
        ]
        return next((value for value in reversed(sessions) if value), None)

    def trace_path(self, trace_id: str) -> Path:
        return self.sessions_dir / f"{_safe_id(trace_id)}.jsonl"

    def prune_expired(self, *, now: datetime | None = None) -> list[Path]:
        cutoff = (now or datetime.now(UTC)) - timedelta(
            days=transcript_retention_days()
        )
        deleted: list[Path] = []
        try:
            paths = list(self.sessions_dir.glob("*.jsonl"))
        except OSError:
            return deleted
        active = self._active_system_path()
        for path in paths:
            if active is not None and path == active:
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
                if modified < cutoff:
                    path.unlink()
                    deleted.append(path)
            except OSError:
                continue
        return deleted

    def usage(self) -> dict[str, Any]:
        total = 0
        try:
            paths = sorted(
                self.sessions_dir.glob("*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            paths = []
        for path in paths:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            total += size
        return {"total_size_bytes": total}

    def _route_trace(self, trace_id: str, item: dict[str, Any]) -> TranscriptRoute:
        path = self.trace_path(trace_id)
        pointer: dict[str, Any] | None = None
        if not path.exists():
            pointer = {
                "kind": "event",
                "type": "session.pointer",
                "timestamp": str(item.get("timestamp") or _now()),
                "trace_id": trace_id,
                "span_id": None,
                "parent_id": None,
                "call_id": None,
                "span": "",
                "source": str(item.get("source") or ""),
                "person_id": str(item.get("person_id") or ""),
                "command": str(item.get("command") or ""),
                "workflow": str(item.get("workflow") or ""),
                "attributes": {
                    **_attributes(item),
                    "session.path": f"sessions/{path.name}",
                },
                "payload": {"path": f"sessions/{path.name}"},
            }
        record = dict(item)
        if _is_finished_boundary(record):
            record = self._with_final_counts(record, path, include=record)
        self._append(path, record)
        index_records = [pointer] if pointer is not None else []
        if _belongs_in_index(record):
            index_records.append(record)
        return TranscriptRoute(index_records=index_records)

    def _route_system(self, item: dict[str, Any]) -> TranscriptRoute:
        session = self._active_system_session()
        if session is None:
            return TranscriptRoute(
                index_records=[item] if _belongs_in_index(item) else []
            )
        index_records: list[dict[str, Any]] = []
        record = dict(item)
        if _is_finished_boundary(record):
            record = self._with_final_counts(record, session.path, include=record)
        self._append(session.path, record)
        if _belongs_in_index(record):
            index_records.append(record)
        return TranscriptRoute(index_records=index_records)

    def _ensure_system_session(self, service_run_id: str) -> tuple[SystemSession, bool]:
        root = self.sessions_dir.resolve()
        with _SYSTEM_LOCK:
            current = _SYSTEM_SESSIONS.get(root)
            if current is not None:
                return current, False
            timestamp = datetime.now(UTC)
            session_id = (
                f"system-{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}"
            )
            session = SystemSession(
                session_id=session_id,
                path=self.sessions_dir / f"{session_id}.jsonl",
                started_at=timestamp.isoformat().replace("+00:00", "Z"),
                service_run_id=service_run_id,
            )
            _SYSTEM_SESSIONS[root] = session
            return session, True

    def _system_boundary_record(
        self,
        event_type: str,
        session: SystemSession,
        *,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        return {
            "kind": "event",
            "type": event_type,
            "timestamp": timestamp or _now(),
            "trace_id": None,
            "span_id": None,
            "parent_id": None,
            "call_id": None,
            "span": "",
            "source": "system",
            "person_id": "",
            "command": "",
            "workflow": "",
            "attributes": {
                "system_session_id": session.session_id,
                "service_run_id": session.service_run_id,
                "session.path": f"sessions/{session.path.name}",
            },
            "payload": {"path": f"sessions/{session.path.name}"},
        }

    def _with_final_counts(
        self,
        item: dict[str, Any],
        path: Path,
        *,
        include: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Other processes and DiagnosticsStore instances may append to the same
        # transcript, so terminal summaries must be calculated from disk.
        counts = TranscriptCounts()
        for existing in self._read(path):
            counts.add(existing)
        if include is not None:
            projected = TranscriptCounts(
                event_count=counts.event_count,
                log_count=counts.log_count,
                error_count=counts.error_count,
                span_count=counts.span_count,
                spans=set(counts.spans),
            )
            projected.add(include)
            final = projected.payload()
        else:
            final = counts.payload()
        record = dict(item)
        record["payload"] = {**_payload(record), **final}
        return record

    def _append(self, path: Path, item: dict[str, Any]) -> None:
        try:
            with _WRITE_LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(item, ensure_ascii=False, default=str) + "\n"
                    )
        except OSError:
            return

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        records: list[dict[str, Any]] = []
        try:
            with _WRITE_LOCK, path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        records.append(item)
        except OSError:
            return []
        return records

    def _active_system_path(self) -> Path | None:
        session = self._active_system_session()
        return session.path if session is not None else None

    def _active_system_session(self) -> SystemSession | None:
        with _SYSTEM_LOCK:
            return _SYSTEM_SESSIONS.get(self.sessions_dir.resolve())


def _belongs_in_index(item: dict[str, Any]) -> bool:
    if _attributes(item).get("github.activity_id"):
        return True
    if item.get("kind") != "event":
        return False
    event_type = str(item.get("type") or "")
    if event_type == "session.pointer" or event_type.startswith("system."):
        return True
    if event_type in {
        "command.started",
        "command.finished",
        "command.failed",
        "member.command.started",
        "member.command.finished",
        "member.command.failed",
        "diagnostics.completed",
        "verify.completed",
        "span.finished",
        "span.failed",
        "scheduler.failed",
        "scheduler.running",
        "scheduler.worker_failed",
    }:
        return True
    return event_type.startswith(("github.", "credential.")) or event_type == (
        "workflow.rate_limited"
    )


def _is_finished_boundary(item: dict[str, Any]) -> bool:
    return str(item.get("type") or "") in {
        "command.finished",
        "command.failed",
        "member.command.finished",
        "member.command.failed",
        "system.finished",
    }


def _attributes(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("attributes")
    return dict(value) if isinstance(value, dict) else {}


def _payload(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("payload")
    return dict(value) if isinstance(value, dict) else {}


def _safe_id(value: str) -> str:
    cleaned = _SAFE_ID.sub("-", value).strip(".-")
    if cleaned and len(cleaned) <= MAX_SESSION_ID_LENGTH:
        return cleaned
    digest = hashlib.sha256(value.encode()).hexdigest()
    return digest


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
