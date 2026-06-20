from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore

from guildbotics.capabilities.task_runs import RUN_ENV, TASK_RUN_ENV
from guildbotics.entities.team import Person
from guildbotics.utils.fileio import (
    get_workspace_data_path,
    load_yaml_file,
    save_yaml_file,
)

Scope = Literal["personal", "team"]
DEFAULT_DIGEST_N = 20
MIN_SECRET_VALUE_LENGTH = 4
RG_NO_MATCH_EXIT_CODE = 1
BODY_FILE = "body.md"
META_FILE = "meta.yml"
RECENT_FILE = "recent.txt"
ARCHIVED_DIR = "archived"
RESERVED_DOC_IDS = {RECENT_FILE, ARCHIVED_DIR}
POLICY_BASELINE_BODY = """- Keep only reusable lessons: pitfalls, solution steps, and design rationale.
- Do not keep trivial logs, temporary trial-and-error, or information useful only for one task.
- Preserve lessons learned from failures. Propose promotion to team memory when the lesson benefits the whole team.
- Keep one topic per document. Write a summary that makes usefulness clear at a glance.
- Add synonyms and English/Japanese variants to keywords so recall can find the note.
- Do not write secrets, tokens, or personal data in memory."""
LOGGER = logging.getLogger(__name__)


class MemberMemoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class PolicyParams:
    digest_n: int = DEFAULT_DIGEST_N


class MemberMemoryService:
    def __init__(self, person: Person) -> None:
        self.person = person
        self.root = get_workspace_data_path("documents")

    def record(
        self,
        *,
        scope: Scope,
        title: str,
        body: str,
        summary: str = "",
        keywords: list[str] | None = None,
        source: list[dict[str, Any]] | None = None,
        pinned: bool = False,
        kind: str = "note",
        policy_approved: bool = False,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kind = _normalize_kind(kind)
        if kind == "policy":
            self._ensure_policy_write_allowed(policy_approved)
            existing = self._team_policy_doc()
            if existing is not None:
                return self.update(
                    doc_id=existing.name,
                    scope="team",
                    title=title,
                    body=body,
                    summary=summary,
                    keywords=keywords,
                    source=source,
                    pinned=True,
                    kind="policy",
                    policy_approved=True,
                    params=params,
                )
            scope = "team"
            pinned = True

        scope_dir = self._scope_dir(scope)
        scope_dir.mkdir(parents=True, exist_ok=True)
        doc_id = self._new_doc_id(scope_dir)
        now = _now()
        meta = {
            "title": title.strip(),
            "summary": summary.strip(),
            "keywords": list(keywords or []),
            "source": list(source or []),
            "created_at": now,
            "updated_at": now,
            "pinned": pinned,
            "kind": kind,
        }
        if params:
            meta.update(params)
        self._write_doc(scope_dir / doc_id, meta, body)
        self._touch_recent(doc_id)
        return self._document_result(scope, scope_dir / doc_id)

    def recall(
        self,
        *,
        queries: list[str],
        meta_only: bool = False,
        limit: int = DEFAULT_DIGEST_N,
    ) -> dict[str, Any]:
        normalized_queries = [query for query in queries if query]
        if normalized_queries:
            results = self._recall_with_rg(
                queries=normalized_queries,
                meta_only=meta_only,
                limit=limit,
            )
            if results is not None:
                return {"results": results}
            LOGGER.warning(
                "ripgrep executable 'rg' was not found; falling back to Python memory recall."
            )
        return {
            "results": self._recall_with_python(normalized_queries, meta_only, limit)
        }

    def _recall_with_rg(
        self,
        *,
        queries: list[str],
        meta_only: bool,
        limit: int,
    ) -> list[dict[str, Any]] | None:
        executable = shutil.which("rg")
        if executable is None:
            return None
        roots = [str(path) for path in self._search_roots()]
        if not roots:
            return []
        command = [
            executable,
            "--json",
            "--fixed-strings",
            "--ignore-case",
            "--hidden",
            "--no-ignore",
            "--glob",
            "!**/archived/**",
            "--glob",
            f"**/{META_FILE}",
        ]
        if not meta_only:
            command.extend(["--glob", f"**/{BODY_FILE}"])
        for query in queries:
            command.extend(["--regexp", query])
        command.extend(roots)
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode == RG_NO_MATCH_EXIT_CODE:
            return []
        if completed.returncode != 0:
            raise MemberMemoryError(
                f"ripgrep memory recall failed: {completed.stderr.strip()}"
            )
        return self._rg_output_to_results(completed.stdout, limit)

    def _rg_output_to_results(self, output: str, limit: int) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        seen_paths: set[Path] = set()
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MemberMemoryError(
                    "ripgrep memory recall returned invalid JSON."
                ) from exc
            if event.get("type") != "match":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            path_payload = data.get("path")
            if not isinstance(path_payload, dict):
                continue
            raw_path = path_payload.get("text")
            if not isinstance(raw_path, str):
                continue
            doc = self._doc_from_match_path(Path(raw_path))
            if doc is None or doc.path in seen_paths:
                continue
            seen_paths.add(doc.path)
            snippet = _rg_line_snippet(data)
            matches.append(_summary_payload(doc, snippet=snippet))
            if len(matches) >= limit:
                break
        return matches

    def _recall_with_python(
        self,
        queries: list[str],
        meta_only: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for scope, doc_dir in self._iter_active_docs():
            doc = self._read_doc(scope, doc_dir)
            haystack = doc.meta_text
            if not meta_only:
                haystack = f"{haystack}\n{doc.body}"
            if queries and not _contains_any(haystack, queries):
                continue
            snippet = _snippet(haystack, queries) if queries else ""
            matches.append(_summary_payload(doc, snippet=snippet))
            if len(matches) >= limit:
                break
        return matches

    def get(self, *, doc_id: str, scope: Scope | None = None) -> dict[str, Any]:
        doc = self._resolve_doc(doc_id, scope)
        payload = _summary_payload(doc)
        payload.update(
            {
                "meta": doc.meta,
                "body": doc.body,
                "assets": [
                    f"documents/{path.relative_to(self.root)}"
                    for path in sorted((doc.path / "assets").glob("**/*"))
                    if path.is_file()
                ],
            }
        )
        return payload

    def update(
        self,
        *,
        doc_id: str,
        scope: Scope | None = None,
        title: str | None = None,
        body: str | None = None,
        summary: str | None = None,
        keywords: list[str] | None = None,
        add_keywords: list[str] | None = None,
        remove_keywords: list[str] | None = None,
        source: list[dict[str, Any]] | None = None,
        pinned: bool | None = None,
        kind: str | None = None,
        policy_approved: bool = False,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        doc = self._resolve_doc(doc_id, scope)
        original_kind = str(doc.meta.get("kind") or "note")
        next_kind = _normalize_kind(kind or original_kind)
        if original_kind == "policy" or next_kind == "policy" or params:
            if next_kind != "policy":
                raise MemberMemoryError("--set is only available for policy memory.")
            self._ensure_policy_write_allowed(policy_approved)
        meta = dict(doc.meta)
        if title is not None:
            meta["title"] = title.strip()
        if summary is not None:
            meta["summary"] = summary.strip()
        if keywords is not None:
            meta["keywords"] = list(keywords)
        else:
            current_keywords = [str(item) for item in meta.get("keywords") or []]
            for keyword in add_keywords or []:
                if keyword not in current_keywords:
                    current_keywords.append(keyword)
            remove_set = set(remove_keywords or [])
            if remove_set:
                current_keywords = [
                    keyword for keyword in current_keywords if keyword not in remove_set
                ]
            meta["keywords"] = current_keywords
        if source is not None:
            meta["source"] = list(source)
        if pinned is not None:
            meta["pinned"] = pinned
        if kind is not None:
            meta["kind"] = next_kind
        if params:
            meta.update(params)
        meta["updated_at"] = _now()
        self._write_doc(doc.path, meta, doc.body if body is None else body)
        self._touch_recent(doc.doc_id)
        return self._document_result(doc.scope, doc.path)

    def touch(self, *, doc_id: str, scope: Scope | None = None) -> dict[str, Any]:
        doc = self._resolve_doc(doc_id, scope)
        self._touch_recent(doc.doc_id)
        return {"doc_id": doc.doc_id, "path": _document_path(doc)}

    def archive(
        self,
        *,
        doc_id: str,
        scope: Scope | None = None,
        policy_approved: bool = False,
    ) -> dict[str, Any]:
        doc = self._resolve_doc(doc_id, scope)
        if str(doc.meta.get("kind") or "note") == "policy":
            self._ensure_policy_write_allowed(policy_approved)
        archived_root = self._scope_dir(doc.scope) / ARCHIVED_DIR
        archived_root.mkdir(parents=True, exist_ok=True)
        target = archived_root / doc.doc_id
        if target.exists():
            raise MemberMemoryError(f"Archived memory already exists: {doc.doc_id}")
        doc.path.rename(target)
        self._remove_recent(doc.doc_id)
        return {
            "doc_id": doc.doc_id,
            "path": f"documents/{target.relative_to(self.root)}",
        }

    def promote(self, *, doc_id: str) -> dict[str, Any]:
        doc = self._resolve_doc(doc_id, "personal")
        if str(doc.meta.get("kind") or "note") == "policy":
            raise MemberMemoryError("Policy memory cannot be promoted.")
        team_dir = self._scope_dir("team")
        team_dir.mkdir(parents=True, exist_ok=True)
        target = team_dir / doc.doc_id
        if target.exists():
            raise MemberMemoryError(f"Team memory already exists: {doc.doc_id}")
        doc.path.rename(target)
        self._touch_recent(doc.doc_id)
        return self._document_result("team", target)

    def load_context_memory(self) -> dict[str, list[dict[str, Any]]]:
        params = self.load_policy_params()
        return {
            "digest": self.load_digest(limit=params.digest_n),
            "pinned": self.load_pinned(),
        }

    def load_digest(self, *, limit: int = DEFAULT_DIGEST_N) -> list[dict[str, Any]]:
        digest: list[dict[str, Any]] = []
        for doc_id in self._read_recent():
            try:
                doc = self._resolve_doc(doc_id, None)
            except MemberMemoryError:
                continue
            digest.append(_summary_payload(doc))
            if len(digest) >= limit:
                return digest
        return digest

    def load_pinned(self) -> list[dict[str, Any]]:
        pinned = [_baseline_policy_payload()]
        for _scope, doc_dir in self._iter_active_docs():
            doc = self._read_doc(_scope, doc_dir)
            if doc.meta.get("pinned") is True:
                payload = _summary_payload(doc)
                payload["body"] = doc.body
                pinned.append(payload)
        return pinned

    def load_policy_params(self) -> PolicyParams:
        policy = self._team_policy_doc()
        if policy is None:
            return PolicyParams()
        doc = self._read_doc("team", policy)
        digest_n = _positive_int(doc.meta.get("digest_n"), DEFAULT_DIGEST_N)
        return PolicyParams(digest_n=digest_n)

    def _document_result(self, scope: Scope, doc_dir: Path) -> dict[str, Any]:
        doc = self._read_doc(scope, doc_dir)
        return _summary_payload(doc)

    def _iter_active_docs(self) -> list[tuple[Scope, Path]]:
        docs: list[tuple[Scope, Path]] = []
        for scope in ("personal", "team"):
            scope_dir = self._scope_dir(scope)
            if not scope_dir.is_dir():
                continue
            for child in sorted(scope_dir.iterdir()):
                if child.name in RESERVED_DOC_IDS or not child.is_dir():
                    continue
                if (child / META_FILE).is_file():
                    docs.append((scope, child))
        return docs

    def _search_roots(self) -> list[Path]:
        return [
            path
            for path in (self._scope_dir("personal"), self._scope_dir("team"))
            if path.is_dir()
        ]

    def _doc_from_match_path(self, path: Path) -> _MemoryDoc | None:
        if path.name not in {META_FILE, BODY_FILE}:
            return None
        doc_dir = path.parent
        if ARCHIVED_DIR in doc_dir.parts:
            return None
        try:
            doc_dir.relative_to(self._scope_dir("personal"))
        except ValueError:
            scope: Scope = "team"
        else:
            scope = "personal"
        if scope == "team":
            try:
                doc_dir.relative_to(self._scope_dir("team"))
            except ValueError:
                return None
        try:
            return self._read_doc(scope, doc_dir)
        except MemberMemoryError:
            return None

    def _read_doc(self, scope: Scope, doc_dir: Path) -> _MemoryDoc:
        meta_path = doc_dir / META_FILE
        if not meta_path.is_file():
            raise MemberMemoryError(f"Memory metadata not found: {doc_dir.name}")
        loaded = load_yaml_file(meta_path)
        if not isinstance(loaded, dict):
            raise MemberMemoryError(f"Memory metadata is invalid: {doc_dir.name}")
        body_path = _body_path(doc_dir)
        body = body_path.read_text(encoding="utf-8") if body_path.is_file() else ""
        doc_id = doc_dir.name
        return _MemoryDoc(
            scope=scope,
            doc_id=doc_id,
            path=doc_dir,
            root=self.root,
            meta=loaded,
            body=body,
            meta_text=yaml.dump(loaded, allow_unicode=True, sort_keys=True),
        )

    def _resolve_doc(self, doc_id: str, scope: Scope | None) -> _MemoryDoc:
        safe_doc_id = _validate_doc_id(doc_id)
        scopes: tuple[Scope, ...] = (scope,) if scope else ("personal", "team")
        for candidate_scope in scopes:
            path = self._scope_dir(candidate_scope) / safe_doc_id
            if path.is_dir() and (path / META_FILE).is_file():
                return self._read_doc(candidate_scope, path)
        raise MemberMemoryError(f"Memory document not found: {doc_id}")

    def _write_doc(self, doc_dir: Path, meta: dict[str, Any], body: str) -> None:
        doc_dir.mkdir(parents=True, exist_ok=True)
        save_yaml_file(doc_dir / META_FILE, meta)
        (doc_dir / BODY_FILE).write_text(_redact_secrets(body), encoding="utf-8")
        (doc_dir / "assets").mkdir(exist_ok=True)

    def _scope_dir(self, scope: Scope) -> Path:
        if scope == "team":
            return self.root / "team"
        return self.root / "personal" / self.person.person_id

    def _recent_path(self) -> Path:
        return self._scope_dir("personal") / RECENT_FILE

    def _read_recent(self) -> list[str]:
        path = self._recent_path()
        if not path.is_file():
            return []
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _write_recent(self, doc_ids: list[str]) -> None:
        path = self._recent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(doc_ids) + ("\n" if doc_ids else ""),
            encoding="utf-8",
        )

    def _touch_recent(self, doc_id: str) -> None:
        current = [item for item in self._read_recent() if item != doc_id]
        self._write_recent([doc_id, *current])

    def _remove_recent(self, doc_id: str) -> None:
        self._write_recent([item for item in self._read_recent() if item != doc_id])

    def _new_doc_id(self, scope_dir: Path) -> str:
        while True:
            doc_id = uuid.uuid4().hex[:12]
            if doc_id not in RESERVED_DOC_IDS and not (scope_dir / doc_id).exists():
                return doc_id

    def _team_policy_doc(self) -> Path | None:
        team_dir = self._scope_dir("team")
        if not team_dir.is_dir():
            return None
        for child in sorted(team_dir.iterdir()):
            if child.name in RESERVED_DOC_IDS or not child.is_dir():
                continue
            meta_path = child / META_FILE
            if not meta_path.is_file():
                continue
            loaded = load_yaml_file(meta_path)
            if isinstance(loaded, dict) and loaded.get("kind") == "policy":
                return child
        return None

    def _ensure_policy_write_allowed(self, policy_approved: bool) -> None:
        if _is_autonomous_run():
            raise MemberMemoryError(
                "Policy memory cannot be changed during autonomous workflow runs."
            )
        if not policy_approved:
            raise MemberMemoryError("Policy memory changes require --policy-approved.")


@dataclass(frozen=True)
class _MemoryDoc:
    scope: Scope
    doc_id: str
    path: Path
    root: Path
    meta: dict[str, Any]
    body: str
    meta_text: str


def _summary_payload(doc: _MemoryDoc, *, snippet: str = "") -> dict[str, Any]:
    payload = {
        "doc_id": doc.doc_id,
        "path": _document_path(doc),
        "title": str(doc.meta.get("title") or ""),
        "summary": str(doc.meta.get("summary") or ""),
    }
    if snippet:
        payload["snippet"] = snippet
    return payload


def _baseline_policy_payload() -> dict[str, Any]:
    return {
        "doc_id": "baseline-policy",
        "path": "documents/team/baseline-policy",
        "title": "Baseline memory policy",
        "summary": "Default memory rules used when no team policy overrides them.",
        "body": POLICY_BASELINE_BODY,
    }


def _document_path(doc: _MemoryDoc) -> str:
    return f"documents/{doc.path.relative_to(doc.root)}"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_doc_id(doc_id: str) -> str:
    clean = doc_id.strip()
    if (
        not clean
        or clean in RESERVED_DOC_IDS
        or "/" in clean
        or "\\" in clean
        or clean in {".", ".."}
    ):
        raise MemberMemoryError("Invalid memory document id.")
    return clean


def _normalize_kind(kind: str) -> str:
    clean = kind.strip() or "note"
    if clean not in {"note", "policy"}:
        raise MemberMemoryError("Memory kind must be note or policy.")
    return clean


def _body_path(doc_dir: Path) -> Path:
    preferred = doc_dir / BODY_FILE
    if preferred.exists():
        return preferred
    candidates = sorted(doc_dir.glob("*.md"))
    return candidates[0] if candidates else preferred


def _contains_any(text: str, queries: list[str]) -> bool:
    lowered = text.casefold()
    return any(query.casefold() in lowered for query in queries)


def _snippet(text: str, queries: list[str], radius: int = 80) -> str:
    lowered = text.casefold()
    for query in queries:
        index = lowered.find(query.casefold())
        if index >= 0:
            start = max(index - radius, 0)
            end = min(index + len(query) + radius, len(text))
            prefix = "..." if start else ""
            suffix = "..." if end < len(text) else ""
            return f"{prefix}{text[start:end].strip()}{suffix}"
    return ""


def _rg_line_snippet(data: dict[str, Any]) -> str:
    lines = data.get("lines")
    if not isinstance(lines, dict):
        return ""
    text = lines.get("text")
    if not isinstance(text, str):
        return ""
    return text.strip()


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _is_autonomous_run() -> bool:
    return bool(os.getenv(TASK_RUN_ENV) or os.getenv(RUN_ENV))


def _redact_secrets(text: str) -> str:
    redacted = text
    for key, value in os.environ.items():
        upper = key.upper()
        if not any(
            part in upper for part in ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY")
        ):
            continue
        if len(value) >= MIN_SECRET_VALUE_LENGTH:
            redacted = redacted.replace(value, "***")
    return redacted
