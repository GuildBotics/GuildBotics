from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from guildbotics.utils.fileio import get_storage_path

TASK_RUN_ENV = "GUILDBOTICS_TASK_RUN_ID"


class TaskRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskRunStatus:
    run_id: str
    completed: bool
    status: str
    summary: str
    evidence_count: int
    evidence_types: list[str]
    completed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "completed": self.completed,
            "status": self.status,
            "summary": self.summary,
            "evidence_count": self.evidence_count,
            "evidence_types": self.evidence_types,
            "completed_at": self.completed_at,
        }


class TaskRunStore:
    WRITE_EVIDENCE_TYPES: ClassVar[set[str]] = {
        "issue_comment",
        "pr_comment",
        "pr_reply",
        "reaction_add",
        "pr_create",
        "git_publish",
        "issue_create",
    }
    COMMENT_EVIDENCE_TYPES: ClassVar[set[str]] = {
        "issue_comment",
        "pr_comment",
        "pr_reply",
    }

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or get_storage_path() / "task-runs"

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
    ) -> TaskRunStatus:
        records = self._read_records(run_id)
        evidence_types = _evidence_types(records)
        if not self._has_required_evidence(status, evidence_types, records):
            raise TaskRunError(
                f"Task run '{run_id}' cannot be completed without required write evidence."
            )
        completed_at = datetime.now(UTC).isoformat()
        self.append(
            run_id,
            {
                "kind": "complete",
                "status": status,
                "summary": summary,
                "ticket_url": ticket_url,
                "person_id": person_id,
                "completed_at": completed_at,
            },
        )
        return self.status(run_id)

    def status(self, run_id: str) -> TaskRunStatus:
        records = self._read_records(run_id)
        evidence_types = _evidence_types(records)
        completions = [record for record in records if record.get("kind") == "complete"]
        if not completions:
            raise TaskRunError(f"Task run '{run_id}' is not completed.")
        latest = completions[-1]
        status = str(latest.get("status", ""))
        if status not in {"done", "asking", "blocked"}:
            raise TaskRunError(f"Task run '{run_id}' has invalid status '{status}'.")
        if not self._has_required_evidence(status, evidence_types, records):
            raise TaskRunError(f"Task run '{run_id}' is missing required evidence.")
        return TaskRunStatus(
            run_id=run_id,
            completed=True,
            status=status,
            summary=str(latest.get("summary", "")),
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

    def _path(self, run_id: str) -> Path:
        safe_run_id = run_id.strip()
        if not safe_run_id or "/" in safe_run_id or "\\" in safe_run_id:
            raise TaskRunError("Invalid task run id.")
        return self.root / f"{safe_run_id}.jsonl"

    def _has_required_evidence(
        self, status: str, evidence_types: list[str], records: list[dict[str, Any]]
    ) -> bool:
        present = set(evidence_types)
        if not present & self.WRITE_EVIDENCE_TYPES:
            return False
        if status == "asking":
            return bool(present & self.COMMENT_EVIDENCE_TYPES)
        if status == "done" and _has_code_publish(records):
            return "pr_create" in present
        return True


def current_task_run_id(explicit: str | None = None) -> str | None:
    return explicit or os.getenv(TASK_RUN_ENV) or None


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
