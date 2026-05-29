from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from guildbotics.entities.team import Person, Team
from guildbotics.utils.fileio import get_storage_path
from guildbotics.utils.memory_repo import MemoryRepository

JsonMap = dict[str, Any]


@dataclass(frozen=True)
class MemoryQuery:
    """Person memory recall request shared by all memory backends."""

    person_id: str
    thread_topic: str
    latest_focus: str
    transcript: str
    source: JsonMap = field(default_factory=dict)
    scope: JsonMap = field(default_factory=dict)
    metadata: JsonMap = field(default_factory=dict)
    retention: JsonMap = field(default_factory=dict)

    def trace_payload(self) -> JsonMap:
        payload: JsonMap = {}
        if self.thread_topic:
            payload["thread_topic"] = self.thread_topic
        if self.latest_focus:
            payload["latest_focus"] = self.latest_focus
        excerpt = self.transcript.strip()
        if excerpt:
            payload["transcript_excerpt"] = excerpt[:1000]
        if self.source:
            payload["source"] = self.source
        if self.scope:
            payload["scope"] = self.scope
        if self.metadata:
            payload["metadata"] = self.metadata
        if self.retention:
            payload["retention"] = self.retention
        return payload


@dataclass(frozen=True)
class MemoryItem:
    """Normalized person memory item returned from a backend recall."""

    id: str
    title: str
    summary: str
    path: str
    content: str
    score: float = 0.0
    match_reason: str = ""
    source: JsonMap = field(default_factory=dict)
    scope: JsonMap = field(default_factory=dict)
    metadata: JsonMap = field(default_factory=dict)
    retention: JsonMap = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryContext:
    """Backend-neutral recall trace passed to workflows and tests."""

    backend: str
    person_id: str = ""
    query: JsonMap = field(default_factory=dict)
    items: list[MemoryItem] = field(default_factory=list)
    status: str = "ok"
    error: JsonMap = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryUpdate:
    """Backend-neutral person memory write request."""

    should_update: bool
    title: str
    summary: str
    memory: str
    topic_id: str = ""
    source: JsonMap = field(default_factory=dict)
    scope: JsonMap = field(default_factory=dict)
    metadata: JsonMap = field(default_factory=dict)
    retention: JsonMap = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryWriteResult:
    """Backend-neutral result of a memory write."""

    changed: bool
    backend: str
    status: str = "ok"
    error: JsonMap = field(default_factory=dict)
    reference: str = ""
    person_id: str = ""
    item_id: str = ""
    title: str = ""
    source: JsonMap = field(default_factory=dict)
    scope: JsonMap = field(default_factory=dict)
    metadata: JsonMap = field(default_factory=dict)
    retention: JsonMap = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryForgetRequest:
    """Backend-neutral person memory forget request."""

    person_id: str
    item_id: str
    reason: str = ""
    source: JsonMap = field(default_factory=dict)
    scope: JsonMap = field(default_factory=dict)
    metadata: JsonMap = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryForgetResult:
    """Backend-neutral result of a memory forget operation."""

    changed: bool
    backend: str
    status: str = "ok"
    error: JsonMap = field(default_factory=dict)
    reference: str = ""
    person_id: str = ""
    item_id: str = ""
    source: JsonMap = field(default_factory=dict)
    scope: JsonMap = field(default_factory=dict)
    metadata: JsonMap = field(default_factory=dict)


class MemoryBackend(Protocol):
    """Personal memory backend contract.

    Backends store and recall person-scoped memories. Chat topic memory is the
    current FileMemoryBackend shape, but callers should depend on the normalized
    MemoryContext and MemoryWriteResult contract so Cognee or fake backends can
    expose the same observable fields.
    """

    def recall(self, query: MemoryQuery) -> MemoryContext: ...

    def remember(self, update: MemoryUpdate) -> MemoryWriteResult: ...

    def forget(self, request: MemoryForgetRequest) -> MemoryForgetResult: ...


class FileMemoryBackend:
    def __init__(self, person: Person, team: Team) -> None:
        self.repo = MemoryRepository(person, team)

    def recall(self, query: MemoryQuery) -> MemoryContext:
        repo_path = self.repo.get_repo_path()
        index = self._load_index(repo_path)
        items = []
        for topic_id, raw in (index.get("topics") or {}).items():
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title", "")).strip()
            summary = str(raw.get("summary", "")).strip()
            path = str(raw.get("path", "")).strip()
            if (
                not path
                or _is_inactive_retention(self._item_map(raw, "retention"))
                or is_expired_retention(self._item_map(raw, "retention"))
                or not self._matches(query, topic_id, title, summary)
            ):
                continue
            memory_path = self._memory_path(repo_path, path)
            if memory_path is None:
                continue
            items.append(
                MemoryItem(
                    id=topic_id,
                    title=title,
                    summary=summary,
                    path=path,
                    content=(
                        memory_path.read_text(encoding="utf-8")
                        if memory_path.exists()
                        else ""
                    ),
                    score=1.0,
                    match_reason=self._match_reason(query, topic_id, title, summary),
                    source=self._item_map(raw, "source"),
                    scope=self._item_map(raw, "scope") or {"person_id": query.person_id},
                    metadata={
                        **self._item_map(raw, "metadata"),
                        "backend_item_id": topic_id,
                    },
                    retention=self._item_map(raw, "retention"),
                )
            )
        return MemoryContext(
            backend="file",
            person_id=query.person_id,
            query=query.trace_payload(),
            items=items,
        )

    def remember(self, update: MemoryUpdate) -> MemoryWriteResult:
        person_id = str(update.scope.get("person_id", "")).strip()
        if not update.should_update or not update.memory.strip():
            return MemoryWriteResult(
                changed=False,
                backend="file",
                person_id=person_id,
                title=update.title,
                source=update.source,
                scope=update.scope,
                metadata=update.metadata,
                retention=update.retention,
            )
        repo_path = self.repo.get_repo_path()
        topic_id = _topic_id(update.topic_id or update.title)
        path = f"topics/{topic_id}/memory.md"
        memory_path = repo_path / path
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        text = f"{update.memory.strip()}\n"

        content_changed = (
            not memory_path.exists()
            or memory_path.read_text(encoding="utf-8") != text
        )
        if content_changed:
            memory_path.write_text(text, encoding="utf-8")

        index = self._load_index(repo_path)
        topics = index.setdefault("topics", {})
        updated_topic: dict[str, Any] = {
            "title": update.title.strip() or topic_id.replace("-", " ").title(),
            "summary": update.summary.strip(),
            "path": path,
        }
        if update.source:
            updated_topic["source"] = update.source
        if update.scope:
            updated_topic["scope"] = update.scope
        if update.metadata:
            updated_topic["metadata"] = update.metadata
        if update.retention:
            updated_topic["retention"] = update.retention

        topic_changed = topics.get(topic_id) != updated_topic
        if topic_changed:
            topics[topic_id] = updated_topic
            (repo_path / "memory_index.yml").write_text(
                yaml.safe_dump(index, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

        changed = content_changed or topic_changed
        commit_sha = self.repo.commit_if_changed("Update chat memory") if changed else None
        return MemoryWriteResult(
            changed=changed,
            backend="file",
            reference=(commit_sha or path) if changed else "",
            person_id=person_id,
            item_id=topic_id,
            title=updated_topic["title"],
            source=update.source,
            scope=update.scope,
            metadata=update.metadata,
            retention=update.retention,
        )

    def forget(self, request: MemoryForgetRequest) -> MemoryForgetResult:
        repo_path = self.repo.get_repo_path()
        index = self._load_index(repo_path)
        topics = index.get("topics")
        if not isinstance(topics, dict) or request.item_id not in topics:
            return MemoryForgetResult(
                changed=False,
                backend="file",
                person_id=request.person_id,
                item_id=request.item_id,
                source=request.source,
                scope=request.scope,
                metadata={**request.metadata, "reason": request.reason},
            )
        topic = topics.get(request.item_id)
        if not isinstance(topic, dict):
            return MemoryForgetResult(
                changed=False,
                backend="file",
                status="failed",
                error={"type": "InvalidMemoryIndex", "message": "topic is not a map"},
                person_id=request.person_id,
                item_id=request.item_id,
                source=request.source,
                scope=request.scope,
                metadata={**request.metadata, "reason": request.reason},
            )
        retention = topic.setdefault("retention", {})
        if not isinstance(retention, dict):
            retention = {}
            topic["retention"] = retention
        if retention.get("status") == "do_not_recall":
            changed = False
        else:
            retention["status"] = "do_not_recall"
            changed = True
        if request.reason:
            retention["reason"] = request.reason
        (repo_path / "memory_index.yml").write_text(
            yaml.safe_dump(index, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        commit_sha = self.repo.commit_if_changed("Forget chat memory") or ""
        return MemoryForgetResult(
            changed=changed,
            backend="file",
            reference=commit_sha,
            person_id=request.person_id,
            item_id=request.item_id,
            source=request.source,
            scope=request.scope,
            metadata={**request.metadata, "reason": request.reason},
        )

    def _load_index(self, repo_path: Path) -> dict[str, Any]:
        index_path = repo_path / "memory_index.yml"
        data = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}

    def _memory_path(self, repo_path: Path, path: str) -> Path | None:
        raw_path = Path(path)
        if raw_path.is_absolute():
            return None
        resolved_repo = repo_path.resolve()
        resolved_path = (repo_path / raw_path).resolve()
        if not resolved_path.is_relative_to(resolved_repo):
            return None
        return resolved_path

    def _matches(
        self, query: MemoryQuery, topic_id: str, title: str, summary: str
    ) -> bool:
        haystack = " ".join([query.thread_topic, query.latest_focus]).casefold()
        needles = [topic_id, title, summary]
        return any(needle and needle.casefold() in haystack for needle in needles)

    def _match_reason(
        self, query: MemoryQuery, topic_id: str, title: str, summary: str
    ) -> str:
        haystack = " ".join([query.thread_topic, query.latest_focus]).casefold()
        for label, needle in [
            ("topic_id", topic_id),
            ("title", title),
            ("summary", summary),
        ]:
            if needle and needle.casefold() in haystack:
                return f"Matched file memory {label}."
        return "Matched file memory index."

    def _item_map(self, raw: dict[str, Any], key: str) -> JsonMap:
        value = raw.get(key)
        return value if isinstance(value, dict) else {}


def write_memory_recall_trace(context: MemoryContext) -> None:
    if not _trace_enabled():
        return
    _append_trace(
        {
            "event": "memory.recall",
            "timestamp": _timestamp(),
            "backend": context.backend,
            "person_id": context.person_id,
            "status": context.status,
            "error": context.error,
            "query": context.query,
            "hits": [
                {
                    "id": item.id,
                    "title": item.title,
                    "score": item.score,
                    "match_reason": item.match_reason,
                    "source": item.source,
                    "scope": item.scope,
                    "metadata": item.metadata,
                    "retention": item.retention,
                }
                for item in context.items
            ],
        }
    )


def write_memory_context_trace(
    *,
    event: str,
    backend: str,
    person_id: str,
    consumer: str,
    query: JsonMap,
    items: list[MemoryItem],
    extra: JsonMap | None = None,
) -> None:
    if not _trace_enabled():
        return
    _append_trace(
        {
            "event": event,
            "timestamp": _timestamp(),
            "backend": backend,
            "person_id": person_id,
            "consumer": consumer,
            "query": query,
            "memory_context": {
                "item_count": len(items),
                "item_ids": [item.id for item in items],
                "items": [
                    {
                        "id": item.id,
                        "title": item.title,
                        "score": item.score,
                        "match_reason": item.match_reason,
                        "source": item.source,
                        "scope": item.scope,
                        "metadata": item.metadata,
                        "retention": item.retention,
                    }
                    for item in items
                ],
            },
            **(extra or {}),
        }
    )


def write_memory_recall_raw_trace(payload: JsonMap) -> None:
    if not _trace_enabled():
        return
    _append_trace({"event": "memory.recall.raw", "timestamp": _timestamp(), **payload})


def write_memory_recall_final_trace(payload: JsonMap) -> None:
    if not _trace_enabled():
        return
    _append_trace({"event": "memory.recall.final", "timestamp": _timestamp(), **payload})


def write_memory_remember_trace(result: MemoryWriteResult) -> None:
    if not _trace_enabled():
        return
    _append_trace(
        {
            "event": "memory.remember",
            "timestamp": _timestamp(),
            "backend": result.backend,
            "person_id": result.person_id,
            "status": result.status,
            "error": result.error,
            "item": {
                "id": result.item_id,
                "title": result.title,
                "source": result.source,
                "scope": result.scope,
                "metadata": result.metadata,
                "retention": result.retention,
            },
            "result": {
                "changed": result.changed,
                "reference": result.reference,
            },
        }
    )


def write_memory_remember_decision_trace(payload: JsonMap) -> None:
    if not _trace_enabled():
        return
    _append_trace(
        {"event": "memory.remember.decision", "timestamp": _timestamp(), **payload}
    )


def write_memory_forget_trace(result: MemoryForgetResult) -> None:
    if not _trace_enabled():
        return
    _append_trace(
        {
            "event": "memory.forget",
            "timestamp": _timestamp(),
            "backend": result.backend,
            "person_id": result.person_id,
            "status": result.status,
            "error": result.error,
            "item": {
                "id": result.item_id,
                "source": result.source,
                "scope": result.scope,
                "metadata": result.metadata,
            },
            "result": {
                "changed": result.changed,
                "reference": result.reference,
            },
        }
    )


def _trace_enabled() -> bool:
    return os.getenv("GUILDBOTICS_MEMORY_TRACE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _trace_path() -> Path:
    configured = os.getenv("GUILDBOTICS_MEMORY_TRACE_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return get_storage_path() / "run" / "memory_trace.jsonl"


def _append_trace(event: JsonMap) -> None:
    path = _trace_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _topic_id(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]+", "-", value.lower()).strip("-")
    if normalized:
        return normalized[:80].strip("-") or "topic"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def _is_inactive_retention(retention: JsonMap) -> bool:
    status = str(retention.get("status", "")).strip().lower()
    return status in {
        "superseded",
        "resolved",
        "archived",
        "do_not_recall",
        "forgotten",
    }


def is_expired_retention(
    retention: JsonMap,
    *,
    now: datetime | None = None,
) -> bool:
    expires_at = str(retention.get("expires_at", "")).strip()
    if not expires_at:
        return False
    parsed = _parse_datetime(expires_at)
    if parsed is None:
        return False
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.now().astimezone().tzinfo)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=current.tzinfo)
    return parsed <= current


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
