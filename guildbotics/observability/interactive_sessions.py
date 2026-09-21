"""Interactive member CLI sessions: their identity on this device and their
shared lifecycle record.

An interactive session is the run of one AI CLI conversation's member commands.
Which commands belong to one session is decided here on the device
(:class:`InteractiveTraceStore`, under ``local/``): a session is keyed by
member, workspace, host and thread and expires when idle. What the session did
is recorded once per session as a shared record (:class:`InteractiveSessionStore`,
``state/sessions/<trace_id>.json``), rewritten at the end of every command, so
another device shows the session from that one record instead of folding the
command events it emitted.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from guildbotics.utils.fileio import (
    get_workspace_local_path,
    get_workspace_state_path,
    iter_json_objects,
)
from guildbotics.utils.timestamps import parse_iso_datetime
from guildbotics.utils.workspace_sync_port import (
    SHARED_RECORD_SCHEMA_VERSION,
    update_shared_json,
)

INTERACTIVE_TRACE_STATE_FILE = "interactive_trace_state.json"
INTERACTIVE_SESSIONS_DIR = "sessions"
INTERACTIVE_SOURCE = "interactive"
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
        return self._path or get_workspace_local_path(
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
            sessions = _active_sessions(_sessions(state), timestamp)
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
            sessions = _active_sessions(_sessions(state), timestamp)
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


class InteractiveSessionStore:
    """The shared lifecycle record of interactive sessions, one file per session.

    Args:
        root (Path | None): The sessions directory, or None for the selected
            workspace's ``state/sessions``.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root or get_workspace_state_path(INTERACTIVE_SESSIONS_DIR)

    def record(
        self,
        session: InteractiveTraceSession,
        *,
        command: str,
        status: str,
        attributes: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Rewrite the session's record with the command that just ended.

        One device writes one session, so there is no concurrent writer to
        lose to; the read-modify-write span is still held because the sync
        queue may check the hub's copy out in between.

        Args:
            session: The session the command ran in.
            command: The command that ended.
            status: How it ended: ``success`` or ``failed``. The session shows
                its last command's result.
            attributes: The attributes the command recorded. Only the
                ``github.*`` ones are kept -- the rest (host, workspace path,
                thread key) describe this machine -- and the session keeps the
                first value it saw for each, which is how a trace names its
                first target.
            now: The time the command ended.
        """
        seen_at = _aware(now).isoformat()

        def _apply(current: Any | None) -> dict[str, Any]:
            record = dict(current) if isinstance(current, dict) else {}
            merged = dict(record.get("attributes") or {})
            for key, value in (attributes or {}).items():
                if str(key).startswith("github.") and value is not None and str(value):
                    merged.setdefault(str(key), str(value))
            record.update(
                {
                    "schema_version": SHARED_RECORD_SCHEMA_VERSION,
                    "trace_id": session.trace_id,
                    "person_id": session.person_id,
                    "source": INTERACTIVE_SOURCE,
                    "command": str(record.get("command") or command),
                    "started_at": str(record.get("started_at") or session.started_at),
                    "last_seen_at": seen_at,
                    "status": status,
                    "attributes": merged,
                }
            )
            return record

        written: dict[str, Any] = update_shared_json(
            self.root / f"{session.trace_id}.json", _apply
        )
        return written

    def list_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Return the sessions active at some point in ``[start, end]``."""
        sessions: list[dict[str, Any]] = []
        for payload in iter_json_objects(self.root, "*.json"):
            started = parse_iso_datetime(str(payload.get("started_at") or ""))
            seen = parse_iso_datetime(str(payload.get("last_seen_at") or ""))
            if started is None or seen is None or started > end or seen < start:
                continue
            sessions.append(payload)
        return sessions


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


def _active_sessions(sessions: dict[str, Any], now: datetime) -> dict[str, Any]:
    active: dict[str, Any] = {}
    for key, value in sessions.items():
        session = _session_from_raw(value)
        if session is not None and not _expired(session, now):
            active[key] = value
    return active


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
