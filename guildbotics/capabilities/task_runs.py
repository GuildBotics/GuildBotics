"""Durable task-run records and completion evidence.

Each run is one shared JSON object at
``state/task-runs/<run_id>/result.json``. A task run has one lifecycle record,
so another device can observe running and terminal states without reconstructing
an event log.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from guildbotics.entities.task_run import (
    TASK_RUN_TERMINAL_STATES,
    TaskRunExecutionMode,
    TaskRunRecord,
    TaskRunResult,
)
from guildbotics.observability import current_trace
from guildbotics.utils.fileio import get_workspace_state_path
from guildbotics.utils.shared_redaction import redact_for_sharing
from guildbotics.utils.shared_write_lock import shared_write_lock
from guildbotics.utils.timestamps import parse_iso_datetime, utc_now_iso
from guildbotics.utils.workspace_sync_port import (
    SHARED_RECORD_SCHEMA_VERSION,
    ChangeSet,
    update_shared_json_with_change,
)
from guildbotics.workspace.identity import ensure_device_identity

RUN_ENV = "GUILDBOTICS_RUN_ID"
TASK_RUN_ENV = "GUILDBOTICS_TASK_RUN_ID"


def chat_event_work_identity(
    service: str, channel_id: str, event_id: str
) -> dict[str, str]:
    """The stable input identity one chat event has as a run.

    The dispatcher claims it before running the workflow and the workflow
    records it when it takes the batch, so both name the same run.
    """
    return {
        "kind": "chat-event",
        "event_id": event_id,
        "service": service,
        "channel_id": channel_id,
    }


def trace_stamp(run_id: str, fallback_source: str = "") -> tuple[str, dict[str, str]]:
    """The source and attributes a run record mirrors from its trace.

    A run is its trace (``run_id == trace_id``), so only that trace may be
    mirrored: a boundary that exits after its trace closed, or inside another
    one, stamps the fallback source and nothing else.
    """
    trace = current_trace()
    if trace is None or trace.trace_id != run_id:
        return fallback_source, {}
    return trace.source, _string_attributes(trace.attributes)


def _string_attributes(attributes: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in attributes.items()
        if value is not None and str(value) != ""
    }


def _first_seen(
    current: Mapping[str, str], incoming: Mapping[str, Any] | None
) -> dict[str, str]:
    """Merge attributes the way a trace is titled: the earliest value wins.

    The incoming values cross the shared boundary here, so this is where they
    are masked and bounded like every other shared payload: a PR title can
    quote a secret value as easily as an error message can.
    """
    merged = dict(current)
    shared: dict[str, Any] = redact_for_sharing(_string_attributes(incoming or {}))
    for key, value in shared.items():
        merged.setdefault(key, str(value))
    return merged


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
        "pr_review",
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
        "pr_review",
        "pr_review_comment",
        "pr_reply",
    }
    CHAT_WRITE_EVIDENCE_TYPES: ClassVar[set[str]] = {
        "chat_reply",
        "chat_post",
        "chat_reaction",
        "chat_noop",
    }
    CHAT_ASKING_EVIDENCE_TYPES: ClassVar[set[str]] = {"chat_reply", "chat_post"}

    def __init__(
        self,
        root: Path | None = None,
        *,
        work_kind: str = "member-work",
        execution_mode: TaskRunExecutionMode = "user_initiated",
        member_id: str = "unknown",
        device_id: str | None = None,
    ) -> None:
        self.root = root or get_workspace_state_path("task-runs")
        self.work_kind = work_kind
        self.execution_mode = execution_mode
        self.member_id = member_id
        self.device_id: str = device_id or _device_id()
        self._completions_cache: list[_RunCompletion] | None = None

    def append(self, run_id: str, record: dict[str, Any]) -> None:
        """Merge one evidence or note into the run's lifecycle record."""
        kind = str(record.get("kind") or "note")
        evidence_type = str(record.get("evidence_type") or "")
        safe_payload = redact_for_sharing(record.get("payload", {}))

        def _update(current: TaskRunRecord) -> TaskRunRecord:
            values: dict[str, Any] = {}
            if record.get("work_kind"):
                values["work_kind"] = str(record["work_kind"])
            if record.get("member_id"):
                values["member_id"] = str(record["member_id"])
            if record.get("device_id"):
                values["device_id"] = str(record["device_id"])
            if record.get("execution_mode") in {
                "autonomous",
                "user_initiated",
                "remote",
            }:
                values["execution_mode"] = record["execution_mode"]
            if kind == "evidence" and evidence_type:
                evidence = list(current.provider_evidence)
                evidence.append(
                    {
                        "recorded_at": datetime.now(UTC).isoformat(),
                        "type": evidence_type,
                        "payload": safe_payload,
                    }
                )
                values["provider_evidence"] = evidence
            elif kind != "evidence" and record.get("message"):
                values["safe_summary"] = redact_for_sharing(str(record["message"]))
            return current.model_copy(update=values)

        self._mutate(run_id, _update)

    def append_evidence(
        self, run_id: str | None, evidence_type: str, payload: dict[str, Any]
    ) -> None:
        if not run_id:
            return
        self.append(
            run_id,
            {"kind": "evidence", "evidence_type": evidence_type, "payload": payload},
        )

    def start_record(
        self,
        run_id: str,
        *,
        work_kind: str,
        execution_mode: TaskRunExecutionMode,
        member_id: str,
        work_identity: dict[str, str] | None = None,
        source: str = "",
        attributes: Mapping[str, Any] | None = None,
    ) -> TaskRunRecord:
        """Create the running record used by the common execution boundary."""

        record, _ = self.start_record_with_change(
            run_id,
            work_kind=work_kind,
            execution_mode=execution_mode,
            member_id=member_id,
            work_identity=work_identity,
            source=source,
            attributes=attributes,
        )
        return record

    def start_record_with_change(
        self,
        run_id: str,
        *,
        work_kind: str,
        execution_mode: TaskRunExecutionMode,
        member_id: str,
        work_identity: dict[str, str] | None = None,
        source: str = "",
        attributes: Mapping[str, Any] | None = None,
    ) -> tuple[TaskRunRecord, ChangeSet | None]:
        """Create a running record and expose its sync notification.

        A run that ended leaving its work undone is re-opened for its next
        attempt instead of being kept terminal, so every attempt of one
        logical unit of work accumulates its evidence under a single run id.
        A run that recorded a result, or whose outcome could not be observed,
        is never restarted. Re-opening moves the record's ownership to the
        device that is starting this attempt, which is the device the run is
        now running on.

        Args:
            source: The source of the run's trace.
            attributes: The trace's attributes at this point; an attribute an
                earlier attempt recorded keeps its earlier value.
        """

        def _start(current: TaskRunRecord) -> TaskRunRecord:
            if current.finished_at and not current.left_work_undone:
                return current
            return current.model_copy(
                update={
                    "work_kind": work_kind,
                    "execution_mode": execution_mode,
                    "member_id": member_id,
                    "device_id": self.device_id,
                    "work_identity": work_identity,
                    "source": source or current.source,
                    "attributes": _first_seen(current.attributes, attributes),
                    "status": "running",
                    "finished_at": None,
                }
            )

        return self._mutate_with_change(run_id, _start)

    def finish_record(
        self,
        run_id: str,
        *,
        status: str,
        safe_summary: str = "",
        attributes: Mapping[str, Any] | None = None,
    ) -> TaskRunRecord | None:
        """Record a generic execution outcome without bypassing evidence rules."""
        record, _ = self.finish_record_with_change(
            run_id, status=status, safe_summary=safe_summary, attributes=attributes
        )
        return record

    def finish_record_with_change(
        self,
        run_id: str,
        *,
        status: str,
        safe_summary: str = "",
        attributes: Mapping[str, Any] | None = None,
    ) -> tuple[TaskRunRecord | None, ChangeSet | None]:
        """Finish a run and expose the sync notification for a barrier.

        A run that never recorded its start took no work, so there is nothing
        to finish and no record is created for it: an input the workflow
        decided not to act on stays absent from the shared history.

        Args:
            attributes: The trace's attributes at the end of the run; the
                attributes recorded at its start keep their values.

        Returns:
            The finished record and its sync notification, or ``(None, None)``
            when the run has no record.
        """
        if status not in TASK_RUN_TERMINAL_STATES:
            raise TaskRunError(f"Task run has invalid terminal status '{status}'.")
        if not self._path(run_id).is_file():
            return None, None

        def _finish(current: TaskRunRecord) -> TaskRunRecord:
            merged = _first_seen(current.attributes, attributes)
            if current.finished_at:
                return current.model_copy(update={"attributes": merged})
            return current.model_copy(
                update={
                    "finished_at": utc_now_iso(),
                    "status": status,
                    "safe_summary": str(redact_for_sharing(safe_summary)),
                    "attributes": merged,
                }
            )

        return self._mutate_with_change(run_id, _finish)

    @property
    def workspace_root(self) -> Path | None:
        """Return the workspace root when this store is backed by a workspace."""
        return self._workspace_root()

    def records(self) -> Iterator[TaskRunRecord]:
        """Read all task records in deterministic order."""
        if not self.root.is_dir():
            return
        for path in sorted(self.root.glob("*/result.json")):
            try:
                yield TaskRunRecord.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise TaskRunError(f"Task run record is invalid: {path}") from exc

    def records_between(self, start: datetime, end: datetime) -> list[TaskRunRecord]:
        """Return the runs that were running at some point in ``[start, end]``.

        This is the timeline's read: a run is one lifecycle record, so the
        screen shows it from this alone instead of folding the events it
        emitted. A record that cannot be read is skipped here rather than
        failing the whole screen; the synchronization boundary reports it.
        """
        matched: list[TaskRunRecord] = []
        for record in self._readable_records():
            started = parse_iso_datetime(record.started_at)
            if started is None or started > end:
                continue
            finished = parse_iso_datetime(record.finished_at or "")
            if finished is not None and finished < start:
                continue
            matched.append(record)
        return matched

    def _readable_records(self) -> Iterator[TaskRunRecord]:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.glob("*/result.json")):
            try:
                yield TaskRunRecord.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue

    def find_by_work_identity(
        self, work_identity: dict[str, str]
    ) -> list[TaskRunRecord]:
        """Find runs for one stable input identity, oldest first."""
        return [
            record for record in self.records() if record.work_identity == work_identity
        ]

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
        # Evidence validation and the result update share one span. A queue
        # checkout cannot replace the evidence between the check and the
        # terminal record, and another writer cannot overwrite the subject
        # metadata after it has been checked.
        with shared_write_lock(self._workspace_root()):
            current = self._read_record_if_exists(run_id)
            evidence = _evidence_records(current)
            evidence_types = _evidence_types(evidence)
            if not self._has_required_evidence(
                status, subject_type, summary, evidence_types, evidence
            ):
                raise TaskRunError(
                    f"Run '{run_id}' cannot be completed without required write evidence."
                )

            result_status = _result_status(status)
            completed_at = datetime.now(UTC).isoformat()

            def _complete(record: TaskRunRecord) -> TaskRunRecord:
                return record.model_copy(
                    update={
                        "work_kind": (
                            record.work_kind
                            if record.work_kind != "member-work"
                            else subject_type
                        ),
                        "member_id": person_id,
                        "finished_at": completed_at,
                        "status": _record_status(status),
                        "safe_summary": str(redact_for_sharing(summary)),
                        "result": TaskRunResult(
                            subject_type=subject_type,
                            subject_id=subject_id,
                            subject_url=subject_url,
                            status=result_status,
                        ),
                    }
                )

            self._mutate(run_id, _complete)
        return self.status(run_id)

    def evidence(self, run_id: str) -> list[dict[str, Any]]:
        """Return evidence in the shape consumed by existing workflow code."""
        records = self._read_record(run_id).provider_evidence
        return [
            {
                "kind": "evidence",
                "evidence_type": str(item.get("type") or ""),
                "payload": item.get("payload")
                if isinstance(item.get("payload"), dict)
                else {},
                "recorded_at": str(item.get("recorded_at") or ""),
                "schema_version": SHARED_RECORD_SCHEMA_VERSION,
            }
            for item in records
            if isinstance(item, dict) and item.get("type")
        ]

    def summaries_by_subject(self) -> dict[tuple[str, str], str]:
        """Map each ``(subject_id, person_id)`` to its latest completion summary."""
        latest: dict[tuple[str, str], tuple[str, str]] = {}
        for completion in self._completions():
            if not completion.subject_id or not completion.summary:
                continue
            key = (completion.subject_id, completion.person_id)
            existing = latest.get(key)
            if existing is None or completion.completed_at >= existing[0]:
                latest[key] = (completion.completed_at, completion.summary)
        return {key: summary for key, (_, summary) in latest.items()}

    def _completions(self) -> list[_RunCompletion]:
        if self._completions_cache is None:
            self._completions_cache = list(self._read_completions())
        return self._completions_cache

    def _read_completions(self) -> Iterator[_RunCompletion]:
        for record in self._readable_records():
            if not record.finished_at:
                continue
            result = _result_subject(record)
            yield _RunCompletion(
                run_id=record.run_id,
                subject_id=result.subject_id if result is not None else "",
                person_id=record.member_id,
                summary=record.safe_summary.strip(),
                completed_at=record.finished_at,
            )

    def status(self, run_id: str) -> RunStatus:
        record = self._read_record(run_id)
        if not record.finished_at or record.status == "running":
            raise TaskRunError(f"Task run '{run_id}' is not completed.")
        result = _result_subject(record)
        status = result.status if result is not None else _public_status(record.status)
        if status not in {"done", "asking", "blocked"}:
            raise TaskRunError(f"Task run '{run_id}' has invalid status '{status}'.")
        subject_type = result.subject_type if result is not None else "ticket"
        evidence = self.evidence(run_id)
        evidence_types = _evidence_types(evidence)
        if not self._has_required_evidence(
            status, subject_type, record.safe_summary, evidence_types, evidence
        ):
            raise TaskRunError(f"Task run '{run_id}' is missing required evidence.")
        return RunStatus(
            run_id=run_id,
            completed=True,
            status=status,
            summary=record.safe_summary,
            subject_type=subject_type,
            subject_id=result.subject_id if result is not None else "",
            subject_url=result.subject_url if result is not None else "",
            person_id=record.member_id,
            evidence_count=len(evidence_types),
            evidence_types=evidence_types,
            completed_at=record.finished_at,
        )

    def _read_record(self, run_id: str) -> TaskRunRecord:
        path = self._path(run_id)
        if not path.is_file():
            raise TaskRunError(f"Task run '{run_id}' was not found.")
        try:
            return TaskRunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TaskRunError(f"Task run '{run_id}' contains invalid JSON.") from exc

    def _read_record_if_exists(self, run_id: str) -> TaskRunRecord:
        path = self._path(run_id)
        if not path.is_file():
            return _new_record(
                run_id,
                work_kind=self.work_kind,
                execution_mode=self.execution_mode,
                member_id=self.member_id,
                device_id=self.device_id,
            )
        return self._read_record(run_id)

    def _mutate(self, run_id: str, mutate: Any) -> TaskRunRecord:
        record, _ = self._mutate_with_change(run_id, mutate)
        return record

    def _mutate_with_change(
        self, run_id: str, mutate: Any
    ) -> tuple[TaskRunRecord, ChangeSet | None]:
        path = self._path(run_id)
        workspace_root = self._workspace_root()
        with shared_write_lock(workspace_root):

            def _apply(data: Any | None) -> dict[str, Any]:
                if data is not None:
                    try:
                        current = TaskRunRecord.model_validate(data)
                    except ValueError as exc:
                        raise TaskRunError(
                            f"Task run '{run_id}' contains an invalid record."
                        ) from exc
                else:
                    current = _new_record(
                        run_id,
                        work_kind=self.work_kind,
                        execution_mode=self.execution_mode,
                        member_id=self.member_id,
                        device_id=self.device_id,
                    )
                updated = mutate(current)
                return updated.model_dump()

            payload, change = update_shared_json_with_change(
                path, _apply, workspace_root=workspace_root
            )
        self._completions_cache = None
        try:
            return TaskRunRecord.model_validate(payload), change
        except ValueError as exc:  # pragma: no cover - callback returns the model
            raise TaskRunError(
                f"Task run '{run_id}' produced an invalid record."
            ) from exc

    def _path(self, run_id: str) -> Path:
        safe_run_id = run_id.strip()
        if not safe_run_id or "/" in safe_run_id or "\\" in safe_run_id:
            raise TaskRunError("Invalid task run id.")
        return self.root / safe_run_id / "result.json"

    def _workspace_root(self) -> Path | None:
        if self.root.parts[-3:] != (".guildbotics", "state", "task-runs"):
            return None
        return self.root.parents[2]

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


def _new_record(
    run_id: str,
    *,
    work_kind: str,
    execution_mode: TaskRunExecutionMode,
    member_id: str,
    device_id: str,
) -> TaskRunRecord:
    return TaskRunRecord(
        run_id=run_id,
        work_kind=work_kind,
        execution_mode=execution_mode,
        member_id=member_id,
        device_id=device_id,
        started_at=utc_now_iso(),
    )


def _device_id() -> str:
    try:
        return ensure_device_identity().device_id
    except Exception:
        return "unknown"


def _record_status(status: str) -> str:
    if status == "done":
        return "succeeded"
    if status in {"asking", "blocked"}:
        return "failed"
    raise TaskRunError(f"Task run has invalid status '{status}'.")


def _result_status(status: str) -> Literal["done", "asking", "blocked"]:
    if status == "done":
        return "done"
    if status == "asking":
        return "asking"
    if status == "blocked":
        return "blocked"
    raise TaskRunError(f"Task run has invalid status '{status}'.")


def _result_subject(record: TaskRunRecord) -> TaskRunResult | None:
    return record.result


def _public_status(status: str) -> str:
    return {"succeeded": "done", "failed": "blocked"}.get(status, status)


def _evidence_records(record: TaskRunRecord) -> list[dict[str, Any]]:
    return [
        {
            "kind": "evidence",
            "evidence_type": str(item.get("type") or ""),
            "payload": item.get("payload")
            if isinstance(item.get("payload"), dict)
            else {},
        }
        for item in record.provider_evidence
        if isinstance(item, dict) and item.get("type")
    ]


def _evidence_types(records: list[dict[str, Any]]) -> list[str]:
    evidence = {
        str(record.get("evidence_type"))
        for record in records
        if record.get("kind") == "evidence" and record.get("evidence_type")
    }
    return sorted(evidence)


def _has_code_publish(records: list[dict[str, Any]]) -> bool:
    return any(
        record.get("kind") == "evidence"
        and record.get("evidence_type") == "git_publish"
        and isinstance(record.get("payload"), dict)
        and record["payload"].get("commit_sha")
        for record in records
    )


TaskRunError = RunError
TaskRunStatus = RunStatus
TaskRunStore = RunStore
