from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from guildbotics.observability import correlation_fields
from guildbotics.utils.fileio import get_workspace_data_path

JsonMap = dict[str, Any]

_trace_lock = threading.Lock()


def prompt_trace_enabled() -> bool:
    return os.getenv("GUILDBOTICS_PROMPT_TRACE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def write_prompt_trace(event: str, payload: JsonMap) -> None:
    if not prompt_trace_enabled():
        return
    item = {
        **_normalize(payload),
        "event": event,
        "timestamp": _timestamp(),
    }
    # Attach the current correlation ids so prompt traces can be aggregated with
    # events/logs under the same trace (and request/response paired via call_id).
    correlation = correlation_fields()
    for key in ("trace_id", "span_id", "parent_id", "call_id", "source"):
        value = correlation.get(key)
        if value:
            item.setdefault(key, value)
    path = prompt_trace_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _trace_lock, path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def prompt_trace_path() -> Path:
    configured = os.getenv("GUILDBOTICS_PROMPT_TRACE_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return get_workspace_data_path("run", "prompt_trace.jsonl")


def read_prompt_trace_events(
    limit: int = 20, path: Path | None = None
) -> tuple[int, list[JsonMap]]:
    path = path or prompt_trace_path()
    if not path.exists():
        return 0, []
    event_count = 0
    events: deque[JsonMap] = deque(maxlen=max(1, limit))
    with _trace_lock, path.open(encoding="utf-8") as fh:
        for line in fh:
            raw = line.strip()
            if not raw:
                continue
            event_count += 1
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                item = {
                    "event": "prompt_trace.parse_error",
                    "timestamp": "",
                    "error": str(exc),
                }
            if isinstance(item, dict):
                events.append(item)
    return event_count, list(reversed(events))


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize(item) for item in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
