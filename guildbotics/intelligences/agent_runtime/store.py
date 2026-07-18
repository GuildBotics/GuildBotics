"""Atomic, versioned persistence for provider-neutral conversation state."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from guildbotics.intelligences.agent_runtime.models import (
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)
from guildbotics.utils.safe_path import safe_path_component

_STORE_VERSION = 1
_DEFAULT_TTL = timedelta(days=7)
_DEFAULT_MAX_TURNS = 100
_DEFAULT_MAX_TOKENS = 1_000_000


class ConversationStore:
    def __init__(
        self,
        workspace_data_root: Path,
        *,
        ttl: timedelta = _DEFAULT_TTL,
        max_turns: int = _DEFAULT_MAX_TURNS,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> None:
        self._root = workspace_data_root / "agent-runtime" / "conversations"
        self._ttl = ttl
        self._max_turns = max(1, max_turns)
        self._max_tokens = max(1, max_tokens)
        self._lock = threading.RLock()

    def resolve(
        self, key: ConversationKey, policy: ResumePolicy, *, model: str = ""
    ) -> ConversationRecord:
        with self._lock:
            record = self.load(key)
            if policy is ResumePolicy.RESUME and (
                record is None or not record.provider_session_id or not record.healthy
            ):
                raise LookupError(
                    "The exact conversation has no healthy provider session to resume."
                )
            if record is None:
                return self._new_record(key, model=model)
            if policy in {ResumePolicy.FRESH, ResumePolicy.RESET}:
                record.rotate(policy.value)
            elif policy is ResumePolicy.AUTO:
                reason = self.rotation_reason(record, model=model)
                if reason:
                    record.rotate(reason)
            if model:
                record.model = model
            return record

    def load(self, key: ConversationKey) -> ConversationRecord | None:
        path = self._path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("version") != _STORE_VERSION:
            return None
        raw_key = payload.get("key")
        if not isinstance(raw_key, dict):
            return None
        try:
            stored_key = ConversationKey(
                person_id=str(raw_key["person_id"]),
                adapter=str(raw_key["adapter"]),
                work_kind=str(raw_key["work_kind"]),
                work_identity=str(raw_key["work_identity"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if stored_key != key:
            return None
        return ConversationRecord(
            key=stored_key,
            generation=_integer(payload.get("generation")),
            provider_session_id=_text(payload.get("provider_session_id")),
            provider_turn_id=_text(payload.get("provider_turn_id")),
            context_cursor=_text(payload.get("context_cursor")),
            last_event_id=_text(payload.get("last_event_id")),
            last_run_id=_text(payload.get("last_run_id")),
            provider=_text(payload.get("provider")),
            model=_text(payload.get("model")),
            healthy=bool(payload.get("healthy", True)),
            turn_count=_integer(payload.get("turn_count")),
            input_tokens=_integer(payload.get("input_tokens")),
            output_tokens=_integer(payload.get("output_tokens")),
            created_at=_text(payload.get("created_at")),
            updated_at=_text(payload.get("updated_at")),
            rotation_reason=_text(payload.get("rotation_reason")),
        )

    def save(self, record: ConversationRecord) -> None:
        with self._lock:
            now = datetime.now(UTC).isoformat()
            record.created_at = record.created_at or now
            record.updated_at = now
            payload = {"version": _STORE_VERSION, **asdict(record)}
            path = self._path(record.key)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary)

    def mark_unhealthy(self, record: ConversationRecord, reason: str) -> None:
        record.healthy = False
        record.rotation_reason = reason
        self.save(record)

    def rotation_reason(self, record: ConversationRecord, *, model: str = "") -> str:
        if not record.healthy:
            return record.rotation_reason or "unhealthy_session"
        if model and record.model and model != record.model:
            return "model_changed"
        if record.turn_count >= self._max_turns:
            return "turn_limit"
        if record.input_tokens + record.output_tokens >= self._max_tokens:
            return "usage_limit"
        updated = _parse_datetime(record.updated_at)
        if updated is not None and datetime.now(UTC) - updated > self._ttl:
            return "ttl_expired"
        return ""

    def _path(self, key: ConversationKey) -> Path:
        return (
            self._root
            / safe_path_component(key.person_id)
            / safe_path_component(key.adapter)
            / f"{key.stable_id}.json"
        )

    @staticmethod
    def _new_record(key: ConversationKey, *, model: str) -> ConversationRecord:
        now = datetime.now(UTC).isoformat()
        return ConversationRecord(key=key, model=model, created_at=now, updated_at=now)


def _text(value: Any) -> str:
    return str(value or "")


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
