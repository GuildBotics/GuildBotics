from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.entities.task_run import TaskRunExecutionMode, TaskRunRecord
from guildbotics.observability import new_id
from guildbotics.runtime.live_state import LivePresentation, LiveStatePort
from guildbotics.runtime.person_lease import (
    PersonExecutionLease,
    PersonLeaseUnavailableError,
)
from guildbotics.runtime.trace_presentations import normalize_trace_presentation
from guildbotics.utils.diagnostics_records import diagnostics_record_scope
from guildbotics.utils.shared_write_lock import shared_write_lock
from guildbotics.utils.workspace_sync_port import (
    ChangeSet,
    NoOpWorkspaceSyncPort,
    get_workspace_sync_port,
)

WorkSource = Literal["manual", "scheduled", "routine", "event_queue"]
WorkRejectionReason = Literal[
    "draining",
    "lease_unavailable",
    "duplicate",
    "sync_unavailable",
    "owner_unreachable",
    "not_owner",
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


class TaskRunSyncUnavailableError(RuntimeError):
    """Raised when a terminal TaskRun cannot be confirmed at the Hub."""

    def __init__(self, record: TaskRunRecord) -> None:
        super().__init__(
            f"TaskRun '{record.run_id}' was saved locally but was not shared."
        )
        self.record = record


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
    "owner_unreachable",
    "not_owner",
    "sync_unavailable",
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
        owner_device_id: str | None = None,
    ) -> Iterator[ActiveWork]:
        """Track one unit of member work for the lifetime of the context.

        Args:
            source: Which runtime path submitted the work.
            person_id: Member the work runs as.
            command: Command label reported in the runtime status.
            work_id: Correlation identity, usually the work's trace id.
            cancel: Called when a forced drain interrupts the work.
            exclusive: Whether to hold the member's execution lease. Read-only
                work that never touches the member's workspace, chat or tickets
                passes ``False`` so it can run alongside scheduled work. It is
                still drained and cancelled like any other tracked work.

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


class ExecutionStatusPublisher:
    """Publish the common execution lifecycle to live state and TaskRunRecord."""

    def __init__(
        self,
        live_state: LiveStatePort | None = None,
        owner_check: Callable[[], bool | None] | None = None,
    ) -> None:
        self._live_state = live_state
        self._owner_check = owner_check

    def set_live_state(self, live_state: LiveStatePort | None) -> None:
        """Attach or detach the process-wide relay adapter."""
        self._live_state = live_state

    def set_owner_check(self, owner_check: Callable[[], bool | None] | None) -> None:
        """Attach the owner probe used for service-derived live updates."""
        self._owner_check = owner_check

    def started(
        self,
        work: ActiveWork,
        *,
        record: bool = True,
        service_work: bool = False,
    ) -> None:
        if self._live_state is not None and self._live_allowed(service_work):
            with suppress(Exception):
                self._live_state.started(
                    work.id,
                    work.id,
                    work.person_id,
                    work.command,
                )
        if record:
            with suppress(Exception):
                RunStore().start_record(
                    work.id,
                    work_kind=work.command,
                    execution_mode=(
                        "user_initiated" if work.source == "manual" else "autonomous"
                    ),
                    member_id=work.person_id,
                    work_identity={"source": work.source, "command": work.command},
                )

    def progressed(
        self,
        work_id: str,
        presentation: Mapping[str, object] | LivePresentation,
        *,
        service_work: bool = False,
    ) -> None:
        if self._live_state is None or not self._live_allowed(service_work):
            return
        with suppress(Exception):
            self._live_state.progressed(work_id, presentation)

    def diagnostics_recorded(
        self,
        work_id: str,
        item: Mapping[str, Any],
        *,
        service_work: bool = False,
    ) -> None:
        """Publish the same presentation the local trace will display."""
        if str(item.get("trace_id") or "") != work_id:
            return
        raw = {str(key): value for key, value in item.items()}
        self.progressed(
            work_id,
            normalize_trace_presentation(raw),
            service_work=service_work,
        )

    def finished(
        self,
        work: ActiveWork,
        status: str,
        *,
        record: bool = True,
        service_work: bool = False,
    ) -> None:
        if self._live_state is not None and self._live_allowed(service_work):
            with suppress(Exception):
                self._live_state.finished(work.id)
        if record:
            with suppress(Exception):
                RunStore().finish_record(
                    work.id,
                    status=status,
                    safe_summary=(
                        "Execution completed."
                        if status == "succeeded"
                        else "Execution failed."
                    ),
                )

    def _live_allowed(self, service_work: bool) -> bool:
        if not service_work or self._owner_check is None:
            return True
        try:
            return self._owner_check() is not False
        except Exception:
            return True


class TaskRunCoordinator(ExecutionCoordinator):
    """Shared execution entry for scheduler, event listener, and manual work."""

    def __init__(
        self,
        status_publisher: ExecutionStatusPublisher | None = None,
        *,
        owner_check: Callable[[], bool | None] | None = None,
    ) -> None:
        super().__init__()
        self._status_publisher = status_publisher or ExecutionStatusPublisher()
        self._owner_check = owner_check
        self._finish_lock = threading.Lock()
        self._pending_finishes: dict[str, ChangeSet] = {}
        self._status_publisher.set_owner_check(owner_check)

    def set_owner_check(self, owner_check: Callable[[], bool | None] | None) -> None:
        """Attach the service-owner probe used by autonomous work."""
        self._owner_check = owner_check
        self._status_publisher.set_owner_check(owner_check)

    def service_owner_state(self) -> bool | None:
        """Return owner authority without collapsing an outage into a stop."""
        return self._owner_state()

    def begin(
        self,
        work_identity: str | Mapping[str, object],
        member_id: str,
        owner_device_id: str,
        *,
        work_kind: str = "workflow",
        execution_mode: TaskRunExecutionMode = "autonomous",
        run_id: str | None = None,
    ) -> BeginResult:
        """Atomically accept a new work identity and publish its start.

        The member/identity scan and record creation share the workspace write
        lock, so two devices cannot both accept the same event or schedule slot
        for one member. The exact write's sync notification is then awaited
        outside that local lock; a failed barrier never starts the caller's
        workflow.
        """
        owner_state = self._owner_state()
        if owner_state is False:
            return BeginResult(False, None, "not_owner")
        if owner_state is None:
            return BeginResult(False, None, "owner_unreachable")

        identity = _work_identity(work_identity)
        store = RunStore(
            work_kind=work_kind,
            execution_mode=execution_mode,
            member_id=member_id,
            device_id=owner_device_id,
        )
        change: ChangeSet | None = None
        with shared_write_lock(store.workspace_root):
            existing = [
                record
                for record in store.find_by_work_identity(identity)
                if record.member_id == member_id and record.status != "interrupted"
            ]
            if existing:
                record = max(existing, key=lambda item: item.started_at)
                if record.finished_at:
                    with self._finish_lock:
                        pending_finish = self._pending_finishes.get(record.run_id)
                    if pending_finish is not None:
                        if not _await_change(pending_finish):
                            return BeginResult(False, record, "sync_unavailable")
                        with self._finish_lock:
                            self._pending_finishes.pop(record.run_id, None)
                reason: BeginReason = (
                    "already_running"
                    if record.status == "running"
                    else "already_finished"
                )
                return BeginResult(False, record, reason)
            accepted_run_id = run_id or new_id()
            record, change = store.start_record_with_change(
                accepted_run_id,
                work_kind=work_kind,
                execution_mode=execution_mode,
                member_id=member_id,
                work_identity=identity,
            )
        if not _await_change(change):
            return BeginResult(False, record, "sync_unavailable")
        return BeginResult(True, record, "started")

    def finish(
        self, run_id: str, terminal_status: str, safe_summary: str = ""
    ) -> TaskRunRecord:
        """Persist a terminal state and wait for its sync notification."""
        store = RunStore()
        record, change = store.finish_record_with_change(
            run_id, status=terminal_status, safe_summary=safe_summary
        )
        if change is not None and not _await_change(change):
            with self._finish_lock:
                self._pending_finishes[run_id] = change
            raise TaskRunSyncUnavailableError(record)
        with self._finish_lock:
            self._pending_finishes.pop(run_id, None)
        return record

    def mark_interrupted(self, previous_owner_device_id: str) -> list[TaskRunRecord]:
        """Mark the previous owner's running workflows before taking over."""
        store = RunStore()
        changes: list[ChangeSet] = []
        interrupted: list[TaskRunRecord] = []
        with shared_write_lock(store.workspace_root):
            for record in store.records():
                if (
                    record.device_id != previous_owner_device_id
                    or record.status != "running"
                ):
                    continue
                updated, change = store.finish_record_with_change(
                    record.run_id,
                    status="interrupted",
                    safe_summary="The previous service owner stopped before completion.",
                )
                interrupted.append(updated)
                if change is not None:
                    changes.append(change)
        for change in changes:
            if not _await_change(change):
                raise TaskRunSyncUnavailableError(interrupted[-1])
        return interrupted

    def _owner_state(self) -> bool | None:
        if self._owner_check is None:
            return True
        try:
            return self._owner_check()
        except Exception:
            return None

    def set_live_state(self, live_state: LiveStatePort | None) -> None:
        """Update the relay adapter without replacing the execution boundary."""
        self._status_publisher.set_live_state(live_state)

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
        owner_device_id: str | None = None,
    ) -> Iterator[ActiveWork]:
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
                    owner_device_id or RunStore().device_id,
                    work_kind=work.command,
                    run_id=work.id,
                )
                if not accepted.accepted:
                    if accepted.reason in {"already_running", "already_finished"}:
                        reason: WorkRejectionReason = "duplicate"
                    elif accepted.reason == "sync_unavailable":
                        reason = "sync_unavailable"
                    elif accepted.reason == "owner_unreachable":
                        reason = "owner_unreachable"
                    else:
                        reason = "not_owner"
                    raise WorkRejectedError(
                        f"Autonomous work was not accepted: {accepted.reason}.",
                        reason=reason,
                        holder=accepted.record,
                    )
            if autonomous and self._owner_state() is False:
                with suppress(Exception):
                    self.finish(
                        work.id,
                        "interrupted",
                        "The service owner changed before execution started.",
                    )
                raise WorkRejectedError(
                    "The service owner changed before execution started.",
                    reason="not_owner",
                )
            self._status_publisher.started(
                work,
                record=not autonomous,
                service_work=autonomous,
            )
            try:
                with diagnostics_record_scope(
                    lambda item: self._status_publisher.diagnostics_recorded(
                        work.id, item, service_work=autonomous
                    )
                ):
                    yield work
            except BaseException as exc:
                status = (
                    "cancelled" if type(exc).__name__ == "CancelledError" else "failed"
                )
                if autonomous:
                    try:
                        self.finish(
                            work.id, status, "Execution was cancelled or failed."
                        )
                    finally:
                        self._status_publisher.finished(
                            work, status, record=False, service_work=True
                        )
                else:
                    self._status_publisher.finished(work, status)
                raise
            else:
                if autonomous:
                    try:
                        self.finish(work.id, "succeeded", "Execution completed.")
                    finally:
                        self._status_publisher.finished(
                            work, "succeeded", record=False, service_work=True
                        )
                else:
                    self._status_publisher.finished(work, "succeeded")


def _work_identity(value: str | Mapping[str, object]) -> dict[str, str]:
    if isinstance(value, str):
        return {"value": value}
    return {str(key): str(item) for key, item in value.items()}


def _await_change(change: ChangeSet | None) -> bool:
    if change is None:
        return True
    port = get_workspace_sync_port()
    if isinstance(port, NoOpWorkspaceSyncPort):
        # A workspace without synchronization has no Hub to await. The local
        # service remains usable; an enabled queue is the case that must prove
        # the start record reached its Hub.
        return True
    return port.await_pushed(change.change_id)
