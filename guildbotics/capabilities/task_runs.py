from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from guildbotics.utils.fileio import get_workspace_data_path

RUN_ENV = "GUILDBOTICS_RUN_ID"
TASK_RUN_ENV = "GUILDBOTICS_TASK_RUN_ID"


@dataclass(frozen=True)
class _RunCompletion:
    run_id: str
    subject_id: str
    person_id: str
    summary: str
    completed_at: str


class RunError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    completed: bool
    status: str
    summary: str
    subject_type: str
    subject_id: str
    subject_url: str
    person_id: str
    evidence_count: int
    evidence_types: list[str]
    completed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "completed": self.completed,
            "status": self.status,
            "summary": self.summary,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "subject_url": self.subject_url,
            "person_id": self.person_id,
            "evidence_count": self.evidence_count,
            "evidence_types": self.evidence_types,
            "completed_at": self.completed_at,
        }


class RunStore:
    TICKET_WRITE_EVIDENCE_TYPES: ClassVar[set[str]] = {
        "issue_comment",
        "pr_comment",
        "pr_review_comment",
        "pr_reply",
        "reaction_add",
        "pr_create",
        "pr_update",
        "git_publish",
        "issue_create",
    }
    TICKET_COMMENT_EVIDENCE_TYPES: ClassVar[set[str]] = {
        "issue_comment",
        "pr_comment",
        "pr_review_comment",
        "pr_reply",
    }
    CHAT_WRITE_EVIDENCE_TYPES: ClassVar[set[str]] = {
        "chat_reply",
        "chat_post",
        "chat_reaction",
        "chat_noop",
    }
    CHAT_ASKING_EVIDENCE_TYPES: ClassVar[set[str]] = {
        "chat_reply",
        "chat_post",
    }

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or get_workspace_data_path("task-runs")
        self._completions_cache: list[_RunCompletion] | None = None

    def append(self, run_id: str, record: dict[str, Any]) -> None:
        path = self._path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "recorded_at": datetime.now(UTC).isoformat(),
            **record,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def append_evidence(
        self, run_id: str | None, evidence_type: str, payload: dict[str, Any]
    ) -> None:
        if not run_id:
            return
        self.append(
            run_id,
            {
                "kind": "evidence",
                "evidence_type": evidence_type,
                "payload": _without_secrets(payload),
            },
        )

    def complete(
        self, run_id: str, status: str, summary: str, ticket_url: str, person_id: str
    ) -> RunStatus:
        return self.complete_run(
            run_id,
            status,
            summary,
            subject_type="ticket",
            subject_id=ticket_url,
            subject_url=ticket_url,
            person_id=person_id,
        )

    def complete_run(
        self,
        run_id: str,
        status: str,
        summary: str,
        *,
        subject_type: str,
        subject_id: str,
        subject_url: str = "",
        person_id: str,
    ) -> RunStatus:
        records = self._read_records_if_exists(run_id)
        evidence_types = _evidence_types(records)
        if not self._has_required_evidence(
            status, subject_type, summary, evidence_types, records
        ):
            raise TaskRunError(
                f"Run '{run_id}' cannot be completed without required write evidence."
            )
        completed_at = datetime.now(UTC).isoformat()
        self.append(
            run_id,
            {
                "kind": "complete",
                "status": status,
                "summary": summary,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "subject_url": subject_url,
                "person_id": person_id,
                "completed_at": completed_at,
            },
        )
        return self.status(run_id)

    def evidence(self, run_id: str) -> list[dict[str, Any]]:
        return [
            record
            for record in self._read_records(run_id)
            if record.get("kind") == "evidence"
        ]

    def summaries_by_subject(self) -> dict[tuple[str, str], str]:
        """Map each ``(subject_id, person_id)`` to its latest completion summary.

        Runs are stored per opaque ``run_id`` but correlate to a domain
        subject (a chat thread event id or a ticket url) via ``subject_id``.
        The person is part of the key because several members can run against
        the same subject (e.g. the same Slack event), so subject alone would
        cross members. Activity history titles a session from the summary the
        matching member wrote. Read-only and tolerant: unreadable or incomplete
        run files are skipped.
        """
        latest: dict[tuple[str, str], tuple[str, str]] = {}
        for completion in self._completions():
            if not completion.subject_id or not completion.summary:
                continue
            key = (completion.subject_id, completion.person_id)
            existing = latest.get(key)
            if existing is None or completion.completed_at >= existing[0]:
                latest[key] = (completion.completed_at, completion.summary)
        return {key: summary for key, (_, summary) in latest.items()}

    def subjects_by_run(self) -> dict[str, str]:
        """Map each ``run_id`` to the domain ``subject_id`` it completed.

        Lets activity history attach run-scoped records that carry only a
        ``run_id`` (e.g. memory writes made by a workflow subprocess, which
        have no trace id) back to the session that owns the same subject.
        """
        return {
            completion.run_id: completion.subject_id
            for completion in self._completions()
            if completion.subject_id
        }

    def _completions(self) -> list[_RunCompletion]:
        """Return all completion records, read from disk once per instance.

        ``summaries_by_subject()`` and ``subjects_by_run()`` are always called
        together (see ``AppRuntime.get_activity_history``); caching here lets
        both share a single filesystem scan instead of re-parsing every
        ``*.jsonl`` file twice.
        """
        if self._completions_cache is None:
            self._completions_cache = list(self._read_completions())
        return self._completions_cache

    def _read_completions(self) -> Iterator[_RunCompletion]:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.glob("*.jsonl")):
            try:
                records = self._read_records_if_exists(path.stem)
            except TaskRunError:
                continue
            for record in records:
                if record.get("kind") != "complete":
                    continue
                yield _RunCompletion(
                    run_id=path.stem,
                    subject_id=str(record.get("subject_id") or ""),
                    person_id=str(record.get("person_id") or ""),
                    summary=str(record.get("summary") or "").strip(),
                    completed_at=str(record.get("completed_at") or ""),
                )

    def status(self, run_id: str) -> RunStatus:
        records = self._read_records(run_id)
        evidence_types = _evidence_types(records)
        completions = [record for record in records if record.get("kind") == "complete"]
        if not completions:
            raise TaskRunError(f"Task run '{run_id}' is not completed.")
        latest = completions[-1]
        status = str(latest.get("status", ""))
        if status not in {"done", "asking", "blocked"}:
            raise TaskRunError(f"Task run '{run_id}' has invalid status '{status}'.")
        subject_type = str(latest.get("subject_type") or "ticket")
        summary = str(latest.get("summary", ""))
        if not self._has_required_evidence(
            status, subject_type, summary, evidence_types, records
        ):
            raise TaskRunError(f"Task run '{run_id}' is missing required evidence.")
        return RunStatus(
            run_id=run_id,
            completed=True,
            status=status,
            summary=summary,
            subject_type=subject_type,
            subject_id=str(latest.get("subject_id") or latest.get("ticket_url") or ""),
            subject_url=str(
                latest.get("subject_url") or latest.get("ticket_url") or ""
            ),
            person_id=str(latest.get("person_id", "")),
            evidence_count=len(evidence_types),
            evidence_types=evidence_types,
            completed_at=str(latest.get("completed_at", "")),
        )

    def _read_records(self, run_id: str) -> list[dict[str, Any]]:
        path = self._path(run_id)
        if not path.is_file():
            raise TaskRunError(f"Task run '{run_id}' was not found.")
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise TaskRunError(
                        f"Task run '{run_id}' contains invalid JSON."
                    ) from exc
                if not isinstance(record, dict):
                    raise TaskRunError(f"Task run '{run_id}' contains invalid records.")
                records.append(record)
        return records

    def _read_records_if_exists(self, run_id: str) -> list[dict[str, Any]]:
        path = self._path(run_id)
        if not path.is_file():
            return []
        return self._read_records(run_id)

    def _path(self, run_id: str) -> Path:
        safe_run_id = run_id.strip()
        if not safe_run_id or "/" in safe_run_id or "\\" in safe_run_id:
            raise TaskRunError("Invalid task run id.")
        return self.root / f"{safe_run_id}.jsonl"

    def _has_required_evidence(
        self,
        status: str,
        subject_type: str,
        summary: str,
        evidence_types: list[str],
        records: list[dict[str, Any]],
    ) -> bool:
        present = set(evidence_types)
        if status == "blocked":
            return bool(summary.strip())
        if subject_type == "chat":
            if status == "asking":
                return bool(present & self.CHAT_ASKING_EVIDENCE_TYPES)
            return bool(present & self.CHAT_WRITE_EVIDENCE_TYPES)
        if not present & self.TICKET_WRITE_EVIDENCE_TYPES:
            return False
        if status == "asking":
            return bool(present & self.TICKET_COMMENT_EVIDENCE_TYPES)
        if status == "done" and _has_code_publish(records):
            return "pr_create" in present
        return True


def current_task_run_id(explicit: str | None = None) -> str | None:
    return explicit or os.getenv(TASK_RUN_ENV) or os.getenv(RUN_ENV) or None


def current_run_id(explicit: str | None = None) -> str | None:
    return explicit or os.getenv(RUN_ENV) or os.getenv(TASK_RUN_ENV) or None


def _evidence_types(records: list[dict[str, Any]]) -> list[str]:
    evidence = {
        str(record.get("evidence_type"))
        for record in records
        if record.get("kind") == "evidence" and record.get("evidence_type")
    }
    return sorted(evidence)


def _without_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        upper = key.upper()
        if any(
            part in upper for part in ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY")
        ):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


def _has_code_publish(records: list[dict[str, Any]]) -> bool:
    for record in records:
        if record.get("kind") != "evidence":
            continue
        if record.get("evidence_type") != "git_publish":
            continue
        payload = record.get("payload")
        if isinstance(payload, dict) and payload.get("commit_sha"):
            return True
    return False


TaskRunError = RunError
TaskRunStatus = RunStatus
TaskRunStore = RunStore
