from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from guildbotics.utils.fileio import get_workspace_data_path
from guildbotics.utils.timestamps import parse_iso_datetime

INTERACTIVE_TRACE_STATE_FILE = "interactive_trace_state.json"
DEFAULT_IDLE_TIMEOUT_MINUTES = 30
_STATE_LOCK = threading.Lock()


@dataclass(frozen=True)
class InteractiveTraceSession:
    trace_id: str
    person_id: str
    workspace: str
    host: str
    thread_key: str
    started_at: str
    last_seen_at: str
    expires_at: str

    @property
    def attributes(self) -> dict[str, str]:
        values = {
            "interactive.kind": "member_cli",
            "interactive.host": self.host,
            "interactive.workspace": self.workspace,
            "interactive.thread_key": self.thread_key,
            "interactive.expires_at": self.expires_at,
        }
        return {key: value for key, value in values.items() if value}


class InteractiveTraceStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        idle_timeout: timedelta = timedelta(minutes=DEFAULT_IDLE_TIMEOUT_MINUTES),
    ) -> None:
        self._path = path
        self._idle_timeout = idle_timeout

    @property
    def path(self) -> Path:
        return self._path or get_workspace_data_path(
            "run", INTERACTIVE_TRACE_STATE_FILE
        )

    def start_or_touch(
        self,
        *,
        person_id: str,
        workspace: str,
        host: str,
        thread_key: str,
        now: datetime | None = None,
    ) -> InteractiveTraceSession:
        timestamp = _aware(now)
        key = _session_key(
            person_id=person_id,
            workspace=workspace,
            host=host,
            thread_key=thread_key,
        )
        with _STATE_LOCK:
            state = self._read_state()
            sessions = _sessions(state)
            current = _session_from_raw(sessions.get(key))
            if current is None or _expired(current, timestamp):
                current = InteractiveTraceSession(
                    trace_id=uuid.uuid4().hex,
                    person_id=person_id,
                    workspace=workspace,
                    host=host,
                    thread_key=thread_key,
                    started_at=timestamp.isoformat(),
                    last_seen_at=timestamp.isoformat(),
                    expires_at=(timestamp + self._idle_timeout).isoformat(),
                )
            else:
                current = _replace_seen(current, timestamp, self._idle_timeout)
            sessions[key] = asdict(current)
            self._write_state({"sessions": sessions})
        return current

    def touch(
        self,
        session: InteractiveTraceSession,
        *,
        now: datetime | None = None,
    ) -> InteractiveTraceSession:
        timestamp = _aware(now)
        key = _session_key(
            person_id=session.person_id,
            workspace=session.workspace,
            host=session.host,
            thread_key=session.thread_key,
        )
        updated = _replace_seen(session, timestamp, self._idle_timeout)
        with _STATE_LOCK:
            state = self._read_state()
            sessions = _sessions(state)
            sessions[key] = asdict(updated)
            self._write_state({"sessions": sessions})
        return updated

    def _read_state(self) -> dict[str, Any]:
        path = self.path
        if not path.is_file():
            return {"sessions": {}}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"sessions": {}}
        return loaded if isinstance(loaded, dict) else {"sessions": {}}

    def _write_state(self, state: dict[str, Any]) -> None:
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f"{path.suffix}.tmp")
            temporary.write_text(
                json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            return


def interactive_host() -> str:
    configured = os.getenv("GUILDBOTICS_INTERACTIVE_HOST", "").strip()
    if configured:
        return configured
    if os.getenv("CODEX_THREAD_ID") or os.getenv("CODEX_SHELL"):
        return "codex"
    if (
        os.getenv("CLAUDE_CODE_SESSION_ID")
        or os.getenv("CLAUDE_SESSION_ID")
        or os.getenv("CLAUDECODE")
    ):
        return "claude_code"
    return "unknown"


def interactive_thread_key() -> str:
    for key in (
        "GUILDBOTICS_INTERACTIVE_THREAD_KEY",
        "CODEX_THREAD_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_SESSION_ID",
    ):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return ""


def _sessions(state: dict[str, Any]) -> dict[str, Any]:
    sessions = state.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def _session_key(*, person_id: str, workspace: str, host: str, thread_key: str) -> str:
    return "\0".join([workspace, person_id, host, thread_key])


def _session_from_raw(value: Any) -> InteractiveTraceSession | None:
    if not isinstance(value, dict):
        return None
    try:
        return InteractiveTraceSession(
            trace_id=str(value["trace_id"]),
            person_id=str(value["person_id"]),
            workspace=str(value["workspace"]),
            host=str(value["host"]),
            thread_key=str(value.get("thread_key", "")),
            started_at=str(value["started_at"]),
            last_seen_at=str(value["last_seen_at"]),
            expires_at=str(value["expires_at"]),
        )
    except KeyError:
        return None


def _expired(session: InteractiveTraceSession, now: datetime) -> bool:
    expires_at = _parse_time(session.expires_at)
    return expires_at is None or now > expires_at


def _replace_seen(
    session: InteractiveTraceSession, timestamp: datetime, idle_timeout: timedelta
) -> InteractiveTraceSession:
    return InteractiveTraceSession(
        trace_id=session.trace_id,
        person_id=session.person_id,
        workspace=session.workspace,
        host=session.host,
        thread_key=session.thread_key,
        started_at=session.started_at,
        last_seen_at=timestamp.isoformat(),
        expires_at=(timestamp + idle_timeout).isoformat(),
    )


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current


def _parse_time(value: str) -> datetime | None:
    return parse_iso_datetime(value)
