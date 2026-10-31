from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from guildbotics.capabilities.task_runs import RunStore, trace_stamp
from guildbotics.entities.task_run import (
    TaskRunExecutionMode,
    TaskRunRecord,
    TaskRunState,
)
from guildbotics.runtime.person_lease import (
    PersonExecutionLease,
    PersonLeaseUnavailableError,
)
from guildbotics.runtime.port import ServiceUnavailable
from guildbotics.utils.correlation import new_id
from guildbotics.utils.shared_write_lock import shared_write_lock

WorkSource = Literal["manual", "scheduled", "routine", "event_queue"]


def failure_status(error: BaseException, source: str) -> TaskRunState:
    """Classify an execution's failure without losing a temporary deferral.

    A manual caller gets its error directly; autonomous work can retry an
    operation that never reached its service on a later cycle.
    """
    if source != "manual" and isinstance(error, ServiceUnavailable):
        return "deferred"
    return "cancelled" if type(error).__name__ == "CancelledError" else "failed"


WorkRejectionReason = Literal[
    "draining",
    "lease_unavailable",
    "duplicate",
]


class WorkRejectedError(RuntimeError):
    """Raised when new work is submitted while the runtime is draining."""

    def __init__(
        self,
        message: str,
        *,
        reason: WorkRejectionReason,
        holder: object | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.holder = holder


@dataclass(frozen=True)
class ActiveWork:
    id: str
    source: WorkSource
    person_id: str
    command: str
    started_at: str


@dataclass(frozen=True)
class _WorkEntry:
    work: ActiveWork
    cancel: Callable[[], None] | None = None


BeginReason = Literal[
    "started",
    "already_running",
    "already_finished",
]


@dataclass(frozen=True)
class BeginResult:
    """The result of accepting one stable service-work identity."""

    accepted: bool
    record: TaskRunRecord | None
    reason: BeginReason

    @property
    def run_id(self) -> str | None:
        """Return the accepted or conflicting run identifier, if any."""
        return self.record.run_id if self.record is not None else None


class ExecutionCoordinator:
    """Tracks currently running member work across scheduler and manual commands."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active: dict[str, _WorkEntry] = {}
        self._draining = False

    @contextmanager
    def track_work(
        self,
        *,
        source: WorkSource,
        person_id: str,
        command: str,
        work_id: str | None = None,
        cancel: Callable[[], None] | None = None,
        exclusive: bool = True,
        work_identity: str | Mapping[str, object] | None = None,
        record_start: bool = True,
    ) -> Iterator[ActiveWork]:
        """Track one unit of member work for the lifetime of the context.

        Args:
            source: Which runtime path submitted the work.
            person_id: Member the work runs as.
            command: Command label reported in the runtime status.
            work_id: Correlation identity, usually the work's trace id.
            cancel: Called when a forced drain interrupts the work.
            exclusive: Whether to hold the member's execution lease to serialize
                runs that use the member's clone. Read-only work passes ``False``
                so it can run alongside scheduled work. It is
                still drained and cancelled like any other tracked work.
            work_identity: The stable input identity a task-run boundary
                claims; this coordinator records nothing and ignores it.
            record_start: Whether a task-run boundary records the run's start
                itself; ignored here.

        Yields:
            ActiveWork: The tracked work entry.

        Raises:
            WorkRejectedError: If the runtime is draining, or the member's
                lease is held by other exclusive work.
        """
        work = ActiveWork(
            id=work_id or new_id(),
            source=source,
            person_id=person_id,
            command=command,
            started_at=datetime.now().astimezone().isoformat(),
        )
        with self._condition:
            if self._draining:
                raise WorkRejectedError(
                    "Runtime is stopping; new work is not accepted.",
                    reason="draining",
                )
        lease = PersonExecutionLease(person_id) if exclusive else None
        if lease is not None:
            try:
                lease.acquire(source=source, command=command, work_id=work.id)
            except PersonLeaseUnavailableError as exc:
                raise WorkRejectedError(
                    str(exc), reason="lease_unavailable", holder=exc.metadata
                ) from exc
        with self._condition:
            if self._draining:
                if lease is not None:
                    lease.release()
                raise WorkRejectedError(
                    "Runtime is stopping; new work is not accepted.",
                    reason="draining",
                )
            self._active[work.id] = _WorkEntry(work=work, cancel=cancel)
            self._condition.notify_all()
        try:
            yield work
        finally:
            with self._condition:
                self._active.pop(work.id, None)
                # Do not clear _draining here: the drain gate must stay closed
                # for the whole stop sequence (begin_drain -> wait_for_drain),
                # otherwise new work could be accepted after the last in-flight
                # work finishes but before the stop completes. wait_for_drain is
                # the sole owner of clearing the flag.
                self._condition.notify_all()
            if lease is not None:
                lease.release()

    def snapshot(self) -> list[ActiveWork]:
        with self._condition:
            return [entry.work for entry in self._active.values()]

    def begin_drain(self, *, force: bool = False) -> None:
        with self._condition:
            self._draining = True
            entries = list(self._active.values()) if force else []
        for entry in entries:
            if entry.cancel is not None:
                entry.cancel()

    def wait_for_drain(self, timeout: float | None = None) -> bool:
        """Wait for active work to finish.

        The drain window always closes when the wait returns, even on timeout:
        a stop that gave up must not keep rejecting work forever, and the
        stuck work is already reported through the runtime's failed status.
        """
        with self._condition:
            drained = self._condition.wait_for(
                lambda: not self._active, timeout=timeout
            )
            self._draining = False
            return drained


class TaskRunCoordinator(ExecutionCoordinator):
    """Shared execution entry for scheduler, event listener, and manual work."""

    def begin(
        self,
        work_identity: str | Mapping[str, object],
        member_id: str,
        *,
        work_kind: str = "workflow",
        execution_mode: TaskRunExecutionMode = "autonomous",
        run_id: str | None = None,
        source: str = "",
        record_start: bool = True,
    ) -> BeginResult:
        """Atomically accept a new work identity and publish its start.

        The member/identity scan and record creation share the workspace write
        lock, so two workers cannot both accept the same event or schedule slot
        for one member.

        With ``record_start`` false the identity is only claimed: the caller
        records the run's start itself once it has decided to act on the
        input, so an input it declines leaves no record.
        The scan still rejects an identity another run holds.

        A run holds an identity while it is running, and keeps holding it
        unless it ended proving that its work did not happen. A run that
        raised or was cancelled left the work undone,
        so the caller's retry is accepted and re-opens that run rather than
        being reported as a duplicate of work that never happened. A
        ``result_unknown`` run keeps its identity: its outcome could not be
        observed, so it is never retried on its own.
        """
        identity = _work_identity(work_identity)
        store = RunStore(
            work_kind=work_kind,
            execution_mode=execution_mode,
            member_id=member_id,
        )
        with shared_write_lock(store.workspace_root):
            existing = [
                record
                for record in store.find_by_work_identity(identity)
                if record.member_id == member_id and not record.left_work_undone
            ]
            if existing:
                record = max(existing, key=lambda item: item.started_at)
                reason: BeginReason = (
                    "already_running"
                    if record.status == "running"
                    else "already_finished"
                )
                return BeginResult(False, record, reason)
            if not record_start:
                return BeginResult(True, None, "started")
            accepted_run_id = run_id or new_id()
            _source, attributes = trace_stamp(accepted_run_id, source)
            record = store.start_record(
                accepted_run_id,
                work_kind=work_kind,
                execution_mode=execution_mode,
                member_id=member_id,
                work_identity=identity,
                source=_source,
                attributes=attributes,
            )
        return BeginResult(True, record, "started")

    def finish(
        self, run_id: str, terminal_status: str, safe_summary: str = ""
    ) -> TaskRunRecord | None:
        """Persist a terminal state.

        Returns:
            The finished record, or None when the run never recorded a start
            (the caller declined the input, so there is nothing to finish).
        """
        store = RunStore()
        _source, attributes = trace_stamp(run_id)
        return store.finish_record(
            run_id,
            status=terminal_status,
            safe_summary=safe_summary,
            attributes=attributes,
        )

    @contextmanager
    def track_work(
        self,
        *,
        source: WorkSource,
        person_id: str,
        command: str,
        work_id: str | None = None,
        cancel: Callable[[], None] | None = None,
        exclusive: bool = True,
        work_identity: str | Mapping[str, object] | None = None,
        record_start: bool = True,
    ) -> Iterator[ActiveWork]:
        """Track autonomous work through the task-run boundary.

        Args:
            record_start: Whether this boundary records the run's start. A
                workflow that decides only after inspecting its input whether
                there is work to do passes ``False`` and records the start
                itself when it takes the work; the boundary then only claims
                the identity and records how the run ended, if it started.
        """
        with super().track_work(
            source=source,
            person_id=person_id,
            command=command,
            work_id=work_id,
            cancel=cancel,
            exclusive=exclusive,
        ) as work:
            autonomous = source != "manual"
            accepted = None
            if autonomous:
                accepted = self.begin(
                    work_identity or {"work_id": work.id},
                    work.person_id,
                    work_kind=work.command,
                    run_id=work.id,
                    source=work.source,
                    record_start=record_start,
                )
                if not accepted.accepted:
                    raise WorkRejectedError(
                        f"Autonomous work was not accepted: {accepted.reason}.",
                        reason="duplicate",
                        holder=accepted.record,
                    )
            else:
                _start_manual_record(work)
            try:
                yield work
            except BaseException as exc:
                status = failure_status(exc, work.source)
                if autonomous:
                    self.finish(
                        work.id,
                        status,
                        "Waiting for an unavailable service."
                        if status == "deferred"
                        else "Execution was cancelled or failed.",
                    )
                else:
                    _finish_manual_record(work, status)
                raise
            else:
                if autonomous:
                    self.finish(work.id, "succeeded", "Execution completed.")
                else:
                    _finish_manual_record(work, "succeeded")


def _start_manual_record(work: ActiveWork) -> None:
    source, attributes = trace_stamp(work.id, work.source)
    with suppress(Exception):
        RunStore().start_record(
            work.id,
            work_kind=work.command,
            execution_mode="user_initiated",
            member_id=work.person_id,
            work_identity={"source": work.source, "command": work.command},
            source=source,
            attributes=attributes,
        )


def _finish_manual_record(work: ActiveWork, status: str) -> None:
    _source, attributes = trace_stamp(work.id)
    with suppress(Exception):
        RunStore().finish_record(
            work.id,
            status=status,
            safe_summary=(
                "Execution completed." if status == "succeeded" else "Execution failed."
            ),
            attributes=attributes,
        )


def _work_identity(value: str | Mapping[str, object]) -> dict[str, str]:
    if isinstance(value, str):
        return {"value": value}
    return {str(key): str(item) for key, item in value.items()}
