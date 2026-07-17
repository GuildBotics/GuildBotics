from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from guildbotics.observability import correlation_fields
from guildbotics.observability.diagnostics_store import DiagnosticsStore

_STORE: DiagnosticsStore | None = None
_STORE_LOCK = threading.Lock()


def _store() -> DiagnosticsStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = DiagnosticsStore()
    return _STORE


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
    _store().record(
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


def record_correlated_io(*, io_type: str, payload: dict[str, Any]) -> None:
    """Persist full request/response content only in the active transcript."""
    correlation = correlation_fields()
    _store().record(
        {
            "kind": "io",
            "type": io_type,
            "trace_id": correlation.get("trace_id"),
            "span_id": correlation.get("span_id"),
            "parent_id": correlation.get("parent_id"),
            "call_id": correlation.get("call_id"),
            "span": correlation.get("span", ""),
            "source": correlation.get("source") or "",
            "person_id": str(correlation.get("person_id") or ""),
            "command": str(correlation.get("command") or ""),
            "workflow": str(correlation.get("workflow") or ""),
            "attributes": dict(correlation.get("attributes") or {}),
            "payload": _normalize(payload),
            "timestamp": datetime.now().astimezone().isoformat(),
        }
    )


def record_correlated_log(*, level: str, message: str) -> None:
    correlation = correlation_fields()
    _store().record(
        {
            "kind": "log",
            "level": level,
            "message": message,
            "trace_id": correlation.get("trace_id"),
            "span_id": correlation.get("span_id"),
            "parent_id": correlation.get("parent_id"),
            "call_id": correlation.get("call_id"),
            "span": correlation.get("span", ""),
            "source": correlation.get("source") or "",
            "person_id": str(correlation.get("person_id") or ""),
            "command": str(correlation.get("command") or ""),
            "workflow": str(correlation.get("workflow") or ""),
            "attributes": dict(correlation.get("attributes") or {}),
            "timestamp": datetime.now().astimezone().isoformat(),
        }
    )


class DiagnosticsLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        record_correlated_log(level=record.levelname, message=self.format(record))


def install_diagnostics_log_handler(logger: logging.Logger) -> DiagnosticsLogHandler:
    existing = next(
        (
            handler
            for handler in logger.handlers
            if isinstance(handler, DiagnosticsLogHandler)
        ),
        None,
    )
    if existing is not None:
        return existing
    handler = DiagnosticsLogHandler()
    logger.addHandler(handler)
    return handler


def record_span_summary(
    *,
    status: str = "finished",
    model: str = "",
    duration_ms: float | None = None,
    usage: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"model": model, "usage": usage or {}}
    if duration_ms is not None:
        payload["duration_ms"] = round(duration_ms, 3)
    record_correlated_event(
        event_type=f"span.{status}",
        payload=payload,
        attributes=attributes,
        default_source="intelligence",
    )


def start_system_session(service_run_id: str = "") -> None:
    store = _store()
    store.start_system_session(service_run_id)
    store.start_maintenance()


def finish_system_session() -> None:
    store = _store()
    store.finish_system_session()
    store.stop_maintenance()


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize(item) for item in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)
