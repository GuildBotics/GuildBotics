from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from guildbotics.entities.team import Person, Team
from guildbotics.utils.memory_repo import MemoryRepository


@dataclass(frozen=True)
class MemoryQuery:
    person_id: str
    thread_topic: str
    latest_focus: str
    transcript: str


@dataclass(frozen=True)
class MemoryItem:
    title: str
    summary: str
    path: str
    content: str


@dataclass(frozen=True)
class MemoryContext:
    backend: str
    items: list[MemoryItem] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryUpdate:
    should_update: bool
    title: str
    summary: str
    memory: str
    topic_id: str = ""


@dataclass(frozen=True)
class MemoryWriteResult:
    changed: bool
    backend: str
    reference: str = ""


class MemoryBackend(Protocol):
    def recall(self, query: MemoryQuery) -> MemoryContext: ...

    def remember(self, update: MemoryUpdate) -> MemoryWriteResult: ...


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
            if not path or not self._matches(query, topic_id, title, summary):
                continue
            memory_path = repo_path / path
            items.append(
                MemoryItem(
                    title=title,
                    summary=summary,
                    path=path,
                    content=(
                        memory_path.read_text(encoding="utf-8")
                        if memory_path.exists()
                        else ""
                    ),
                )
            )
        return MemoryContext(backend="file", items=items)

    def remember(self, update: MemoryUpdate) -> MemoryWriteResult:
        if not update.should_update or not update.memory.strip():
            return MemoryWriteResult(changed=False, backend="file")
        repo_path = self.repo.get_repo_path()
        topic_id = _topic_id(update.topic_id or update.title)
        path = f"topics/{topic_id}/memory.md"
        memory_path = repo_path / path
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        text = f"{update.memory.strip()}\n"
        if memory_path.exists() and memory_path.read_text(encoding="utf-8") == text:
            return MemoryWriteResult(changed=False, backend="file", reference=path)
        memory_path.write_text(text, encoding="utf-8")
        index = self._load_index(repo_path)
        topics = index.setdefault("topics", {})
        topics[topic_id] = {
            "title": update.title.strip() or topic_id.replace("-", " ").title(),
            "summary": update.summary.strip(),
            "path": path,
        }
        (repo_path / "memory_index.yml").write_text(
            yaml.safe_dump(index, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        commit_sha = self.repo.commit_if_changed("Update chat memory") or ""
        return MemoryWriteResult(changed=True, backend="file", reference=commit_sha or path)

    def _load_index(self, repo_path: Path) -> dict[str, Any]:
        index_path = repo_path / "memory_index.yml"
        data = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}

    def _matches(
        self, query: MemoryQuery, topic_id: str, title: str, summary: str
    ) -> bool:
        haystack = " ".join([query.thread_topic, query.latest_focus]).casefold()
        needles = [topic_id, title, summary]
        return any(needle and needle.casefold() in haystack for needle in needles)


def _topic_id(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]+", "-", value.lower()).strip("-")
    if normalized:
        return normalized[:80].strip("-") or "topic"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
