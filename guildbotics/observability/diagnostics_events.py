from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from guildbotics.observability import correlation_fields
from guildbotics.observability.activity_event_store import (
    ActivityEventStore,
    is_domain_activity_event,
)
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.utils.fileio import get_workspace_config_dir, get_workspace_local_path
from guildbotics.utils.secret_store import KeyringSecretStore
from guildbotics.utils.shared_redaction import workspace_secret_values

_STORE: DiagnosticsStore | None = None
_STORE_LOCK = threading.Lock()


def load_required_io_redaction_values() -> tuple[str, ...]:
    """Load the secrets used to redact one group of required IO records."""
    store = KeyringSecretStore(get_workspace_config_dir())
    secrets = set(workspace_secret_values())
    keys = store.keys()
    secrets.update(value for key in keys if (value := store.get(key)))
    return tuple(sorted(secrets, key=len, reverse=True))


def record_required_io(
    record_id: str,
    payload: dict[str, Any],
    *,
    redaction_values: tuple[str, ...] | None = None,
) -> Path:
    """Persist a replayable local IO artifact, propagating every storage failure.

    Unlike optional session transcripts this is an execution prerequisite.
    The caller supplies an opaque hex ID; no input becomes a filesystem path.
    Secret values are masked without truncating the rest of the input.
    """
    if not record_id or any(ch not in "0123456789abcdef" for ch in record_id):
        raise ValueError("Invalid IO record ID")
    path = get_workspace_local_path("run", "required-io", f"{record_id}.json")
    text = json.dumps(
        {
            **correlation_fields(),
            "timestamp": datetime.now().astimezone().isoformat(),
            "payload": payload,
        },
        ensure_ascii=False,
        default=str,
    )
    ordered_secrets = (
        load_required_io_redaction_values()
        if redaction_values is None
        else redaction_values
    )

    def mask(value: Any) -> Any:
        if isinstance(value, str):
            for secret in ordered_secrets:
                value = value.replace(secret, "***")
        elif isinstance(value, dict):
            return {mask(key): mask(item) for key, item in value.items()}
        elif isinstance(value, list):
            return [mask(item) for item in value]
        return value

    text = json.dumps(mask(json.loads(text)), ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return path


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
    record = {
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
    if is_domain_activity_event(event_type):
        ActivityEventStore().record(record)
    _store().record(record)


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
    effort: str = "",
    duration_ms: float | None = None,
    usage: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"model": model, "effort": effort, "usage": usage or {}}
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
