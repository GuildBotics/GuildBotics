from __future__ import annotations

import threading

import pytest

from guildbotics.drivers.execution import (
    ExecutionCoordinator,
    ExecutionStatusPublisher,
    TaskRunCoordinator,
    WorkRejectedError,
)
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.utils.diagnostics_records import diagnostics_record_scope
from guildbotics.utils.workspace_sync_port import set_workspace_sync_port


class _BarrierPort:
    def __init__(self, *, shared: bool = True) -> None:
        self.shared = shared
        self.changes: list[str] = []

    def shared_state_changed(self, change) -> bool:
        self.changes.append(change.change_id)
        return True

    def await_pushed(self, change_id: str) -> bool:
        return self.shared


class _LivePort:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.presentations: list[tuple[str, object]] = []

    def started(self, work_id, run_id, member_id, workflow_name) -> None:
        self.events.append(("started", work_id))

    def progressed(self, work_id, presentation, retry_at=None) -> None:
        self.events.append(("progressed", work_id))
        self.presentations.append((work_id, presentation))

    def finished(self, work_id) -> None:
        self.events.append(("finished", work_id))


def _start_tracked_work(
    coordinator: ExecutionCoordinator,
    release: threading.Event,
    cancelled: threading.Event | None = None,
) -> threading.Thread:
    entered = threading.Event()

    def _work() -> None:
        with coordinator.track_work(
            source="manual",
            person_id="alice",
            command="demo",
            cancel=cancelled.set if cancelled is not None else None,
        ):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=_work)
    thread.start()
    assert entered.wait(timeout=5)
    return thread


def test_track_work_snapshot_and_completion() -> None:
    coordinator = ExecutionCoordinator()
    release = threading.Event()
    thread = _start_tracked_work(coordinator, release)

    snapshot = coordinator.snapshot()
    assert [(work.source, work.person_id, work.command) for work in snapshot] == [
        ("manual", "alice", "demo")
    ]

    release.set()
    thread.join(timeout=5)
    assert coordinator.snapshot() == []


def test_person_lease_rejects_same_person_and_allows_different_person() -> None:
    coordinator = ExecutionCoordinator()
    release = threading.Event()
    thread = _start_tracked_work(coordinator, release)
    try:
        with (
            pytest.raises(WorkRejectedError) as excinfo,
            coordinator.track_work(
                source="event_queue", person_id="alice", command="chat"
            ),
        ):
            pass
        assert excinfo.value.holder is not None
        assert excinfo.value.reason == "lease_unavailable"
        with coordinator.track_work(
            source="event_queue", person_id="bob", command="chat"
        ):
            pass
    finally:
        release.set()
        thread.join(timeout=5)


def test_begin_drain_rejects_new_work_until_drained() -> None:
    coordinator = ExecutionCoordinator()
    release = threading.Event()
    thread = _start_tracked_work(coordinator, release)
    coordinator.begin_drain()

    with (
        pytest.raises(WorkRejectedError) as excinfo,
        coordinator.track_work(source="manual", person_id="alice", command="rejected"),
    ):
        pass
    assert excinfo.value.reason == "draining"

    release.set()
    assert coordinator.wait_for_drain(timeout=5) is True
    thread.join(timeout=5)

    with coordinator.track_work(source="manual", person_id="alice", command="next"):
        pass


def test_drain_gate_stays_closed_until_wait_for_drain() -> None:
    coordinator = ExecutionCoordinator()
    release = threading.Event()
    thread = _start_tracked_work(coordinator, release)
    coordinator.begin_drain()

    # Finish the in-flight work but do not call wait_for_drain yet: the drain
    # gate must stay closed for the whole stop sequence, so new work is still
    # rejected even though nothing is currently active.
    release.set()
    thread.join(timeout=5)
    with (
        pytest.raises(WorkRejectedError),
        coordinator.track_work(source="manual", person_id="alice", command="x"),
    ):
        pass

    # wait_for_drain is the sole owner of reopening the gate.
    assert coordinator.wait_for_drain(timeout=5) is True
    with coordinator.track_work(source="manual", person_id="alice", command="y"):
        pass


def test_forced_drain_cancels_active_work() -> None:
    coordinator = ExecutionCoordinator()
    release = threading.Event()
    cancelled = threading.Event()
    thread = _start_tracked_work(coordinator, release, cancelled)

    coordinator.begin_drain(force=True)

    assert cancelled.wait(timeout=5)
    release.set()
    thread.join(timeout=5)


def test_wait_for_drain_timeout_closes_drain_window() -> None:
    coordinator = ExecutionCoordinator()
    release = threading.Event()
    thread = _start_tracked_work(coordinator, release)
    coordinator.begin_drain()

    assert coordinator.wait_for_drain(timeout=0.05) is False

    # The drain window closes, but the still-running Alice work retains its
    # cross-process lease. Work for another person is accepted immediately.
    with coordinator.track_work(source="manual", person_id="bob", command="next"):
        pass

    release.set()
    thread.join(timeout=5)


def _start_non_exclusive_work(
    coordinator: ExecutionCoordinator, release: threading.Event
) -> threading.Thread:
    entered = threading.Event()

    def _work() -> None:
        with coordinator.track_work(
            source="manual",
            person_id="alice",
            command="troubleshoot:trace-1",
            exclusive=False,
        ):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=_work)
    thread.start()
    assert entered.wait(timeout=5)
    return thread


def test_non_exclusive_work_runs_alongside_the_same_person_lease() -> None:
    coordinator = ExecutionCoordinator()
    release = threading.Event()
    thread = _start_tracked_work(coordinator, release)
    try:
        # A read-only assistant turn must stay usable while the member it runs
        # as is busy: that is exactly when its logs are worth investigating.
        with coordinator.track_work(
            source="manual",
            person_id="alice",
            command="troubleshoot:trace-1",
            exclusive=False,
        ):
            assert {work.command for work in coordinator.snapshot()} == {
                "demo",
                "troubleshoot:trace-1",
            }
    finally:
        release.set()
        thread.join(timeout=5)


def test_non_exclusive_work_does_not_hold_the_person_lease() -> None:
    coordinator = ExecutionCoordinator()
    release = threading.Event()
    thread = _start_non_exclusive_work(coordinator, release)
    try:
        with coordinator.track_work(
            source="routine", person_id="alice", command="scheduled"
        ):
            pass
    finally:
        release.set()
        thread.join(timeout=5)
    assert coordinator.snapshot() == []


def test_non_exclusive_work_is_still_rejected_while_draining() -> None:
    coordinator = ExecutionCoordinator()
    coordinator.begin_drain()

    with (
        pytest.raises(WorkRejectedError) as caught,
        coordinator.track_work(
            source="manual",
            person_id="alice",
            command="troubleshoot:trace-1",
            exclusive=False,
        ),
    ):
        pass

    assert caught.value.reason == "draining"


def test_task_run_begin_deduplicates_terminal_work_and_finishes() -> None:
    port = _BarrierPort()
    set_workspace_sync_port(port)
    try:
        coordinator = TaskRunCoordinator()
        first = coordinator.begin("event-1", "alice", "device-1")
        assert first.accepted is True
        assert first.record is not None
        assert first.record.status == "running"

        duplicate = coordinator.begin("event-1", "alice", "device-2")
        assert duplicate.accepted is False
        assert duplicate.reason == "already_running"

        finished = coordinator.finish(first.run_id or "", "succeeded", "done")
        assert finished.status == "succeeded"
        terminal = coordinator.begin("event-1", "alice", "device-2")
        assert terminal.accepted is False
        assert terminal.reason == "already_finished"
    finally:
        set_workspace_sync_port(None)


def test_task_run_mark_interrupted_allows_a_new_identity_run() -> None:
    port = _BarrierPort()
    set_workspace_sync_port(port)
    try:
        coordinator = TaskRunCoordinator()
        started = coordinator.begin("event-2", "alice", "old-device")
        assert started.accepted is True
        interrupted = coordinator.mark_interrupted("old-device")
        assert [record.status for record in interrupted] == ["interrupted"]
        retried = coordinator.begin("event-2", "alice", "new-device")
        assert retried.accepted is True
    finally:
        set_workspace_sync_port(None)


def test_task_run_begin_does_not_accept_when_start_barrier_fails() -> None:
    port = _BarrierPort(shared=False)
    set_workspace_sync_port(port)
    try:
        result = TaskRunCoordinator().begin("event-3", "alice", "device-1")
        assert result.accepted is False
        assert result.reason == "sync_unavailable"
    finally:
        set_workspace_sync_port(None)


def test_autonomous_work_publishes_started_and_finished_live_events() -> None:
    port = _BarrierPort()
    live = _LivePort()
    set_workspace_sync_port(port)
    try:
        coordinator = TaskRunCoordinator(ExecutionStatusPublisher(live))
        with coordinator.track_work(
            source="routine",
            person_id="alice",
            command="workflow",
            work_id="work-live",
            work_identity="event-live",
        ):
            pass
        assert live.events == [("started", "work-live"), ("finished", "work-live")]
    finally:
        set_workspace_sync_port(None)


def test_diagnostics_record_publishes_the_local_trace_presentation() -> None:
    live = _LivePort()
    publisher = ExecutionStatusPublisher(live)
    record = {
        "kind": "io",
        "trace_id": "work-live",
        "type": "llm.request",
        "payload": {"prompt": "Think about the next step."},
    }

    with diagnostics_record_scope(
        lambda item: publisher.diagnostics_recorded("work-live", item)
    ):
        DiagnosticsStore().record(record)

    assert [work_id for work_id, _ in live.presentations] == ["work-live"]
    presentation = live.presentations[0][1]
    assert presentation.message == "Think about the next step."
    assert presentation.label_key.endswith("llm_request")


def test_autonomous_work_checks_owner_before_command_start() -> None:
    states = iter([True, False])
    coordinator = TaskRunCoordinator(owner_check=lambda: next(states))

    with (
        pytest.raises(WorkRejectedError) as excinfo,
        coordinator.track_work(
            source="routine",
            person_id="alice",
            command="workflow",
            work_id="work-owner-change",
            work_identity="event-owner-change",
        ),
    ):
        pass

    assert excinfo.value.reason == "not_owner"
