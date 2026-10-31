from __future__ import annotations

import threading

import pytest

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.drivers.execution import (
    ExecutionCoordinator,
    TaskRunCoordinator,
    WorkRejectedError,
)
from guildbotics.entities.task_run import TASK_RUN_TERMINAL_STATES
from guildbotics.runtime.port import ServiceUnavailable
from guildbotics.drivers.utils import command_boundary
from guildbotics.observability.trace_status import TraceStatus, resolve_trace_status
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.utils.correlation import trace_scope
from guildbotics.utils.diagnostics_records import diagnostics_record_scope


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
    coordinator = TaskRunCoordinator()
    first = coordinator.begin("event-1", "alice")
    assert first.accepted is True
    assert first.record is not None
    assert first.record.status == "running"

    duplicate = coordinator.begin("event-1", "alice")
    assert duplicate.accepted is False
    assert duplicate.reason == "already_running"

    finished = coordinator.finish(first.run_id or "", "succeeded", "done")
    assert finished.status == "succeeded"
    terminal = coordinator.begin("event-1", "alice")
    assert terminal.accepted is False
    assert terminal.reason == "already_finished"


def test_task_run_begin_allows_same_identity_for_different_members() -> None:
    coordinator = TaskRunCoordinator()
    first = coordinator.begin("event-shared", "alice")
    assert first.accepted is True
    coordinator.finish(first.run_id or "", "succeeded", "done")

    second = coordinator.begin("event-shared", "bob")
    assert second.accepted is True
    assert second.record is not None
    assert second.record.member_id == "bob"


@pytest.mark.parametrize("terminal_status", sorted(TASK_RUN_TERMINAL_STATES))
def test_task_run_begin_retries_every_terminal_state_that_left_work_undone(
    terminal_status: str,
) -> None:
    """Only a state that proves the work did not happen releases an identity.

    Parametrizing over the terminal states keeps this a statement about the
    whole population: a state added later has to be classified here, as one
    that frees its input for another attempt or one that keeps it.
    """
    coordinator = TaskRunCoordinator()
    started = coordinator.begin("event-retry", "alice", run_id="run-retry")
    assert started.accepted is True
    RunStore().append_evidence("run-retry", "chat_reply", {"text": "partial"})
    coordinator.finish("run-retry", terminal_status, "stopped")

    retried = coordinator.begin("event-retry", "alice", run_id="run-retry")

    if terminal_status in {"succeeded", "result_unknown"}:
        # ``succeeded`` recorded the outcome; ``result_unknown`` means the
        # outcome could not be observed, so the work may already have taken
        # effect and is never repeated without the user asking for it.
        assert retried.accepted is False
        assert retried.reason == "already_finished"
        return
    # Nothing acted on the work, so the next attempt runs under the same
    # run id and keeps the evidence earlier attempts already recorded.
    assert retried.accepted is True
    assert retried.record is not None
    assert retried.record.run_id == "run-retry"
    assert retried.record.status == "running"
    assert retried.record.finished_at is None
    assert len(retried.record.provider_evidence) == 1


@pytest.mark.parametrize("source", ["routine", "scheduled", "manual"])
def test_unavailable_work_closes_as_deferred_without_a_failure_record(source):
    coordinator = TaskRunCoordinator()
    events = []
    with (
        pytest.raises(ServiceUnavailable),
        diagnostics_record_scope(events.append),
        trace_scope(source, trace_id="unavailable-work", person_id="aiko"),
        coordinator.track_work(
            source=source,
            person_id="aiko",
            command="ticket",
            work_id="unavailable-work",
        ),
        command_boundary(command_name="ticket", task_type=source, person_id="aiko"),
    ):
        raise ServiceUnavailable("not connected")
    [record] = RunStore().records()
    assert record.status == ("failed" if source == "manual" else "deferred")
    assert record.finished_at is not None
    assert record.left_work_undone
    status = TraceStatus()
    status.observe(record.status)
    assert status.resolve() == ("failed" if source == "manual" else "info")
    assert resolve_trace_status(events) == ("failed" if source == "manual" else "info")


def test_task_run_begin_keeps_a_recorded_asking_completion_terminal() -> None:
    """An ``asking`` completion is stored as ``failed`` but is still an outcome."""
    coordinator = TaskRunCoordinator()
    started = coordinator.begin("event-asking", "alice", run_id="run-asking")
    assert started.accepted is True
    store = RunStore()
    store.append_evidence("run-asking", "chat_reply", {"text": "which one?"})
    completed = store.complete_run(
        "run-asking",
        "asking",
        "asked the requester",
        subject_type="chat",
        subject_id="slack:C1:100.1",
        person_id="alice",
    )
    assert completed.status == "asking"

    again = coordinator.begin("event-asking", "alice", run_id="run-asking")

    assert again.accepted is False
    assert again.reason == "already_finished"


def test_claim_only_begin_records_nothing_and_leaves_nothing_to_finish() -> None:
    """A workflow that records its own start leaves no record when it declines."""
    coordinator = TaskRunCoordinator()
    claimed = coordinator.begin(
        "event-claim", "alice", run_id="run-claim", record_start=False
    )
    assert claimed.accepted is True
    assert claimed.record is None
    assert list(RunStore().records()) == []

    # The boundary still finishes the run when the workflow took the work.
    assert coordinator.finish("run-claim", "succeeded", "done") is None
    assert list(RunStore().records()) == []


def test_claim_only_begin_still_rejects_a_held_identity() -> None:
    coordinator = TaskRunCoordinator()
    assert coordinator.begin("event-held", "alice").accepted is True
    duplicate = coordinator.begin("event-held", "alice", record_start=False)
    assert duplicate.accepted is False
    assert duplicate.reason == "already_running"


def test_begin_and_finish_mirror_the_runs_trace_into_its_record() -> None:
    """The record names the run's work, without the trace."""
    coordinator = TaskRunCoordinator()
    with trace_scope(
        "routine",
        trace_id="run-trace",
        attributes={"target.url": "https://github.com/o/r/issues/1"},
    ) as trace:
        started = coordinator.begin(
            "event-trace", "alice", run_id="run-trace", source="x"
        )
        assert started.record is not None
        assert started.record.source == "routine"
        assert started.record.attributes == {
            "target.url": "https://github.com/o/r/issues/1"
        }
        trace.attributes["target.title"] = "Fix it"
        trace.attributes["target.url"] = "https://github.com/o/r/pull/2"
        finished = coordinator.finish("run-trace", "succeeded", "done")
    assert finished is not None
    # What the trace learned is added; what it started with is kept.
    assert finished.attributes == {
        "target.url": "https://github.com/o/r/issues/1",
        "target.title": "Fix it",
    }


def test_begin_outside_the_runs_trace_stamps_only_the_fallback_source() -> None:
    with trace_scope("manual", trace_id="another", attributes={"k": "v"}):
        started = TaskRunCoordinator().begin(
            "event-other", "alice", run_id="run-other", source="routine"
        )
    assert started.record is not None
    assert started.record.source == "routine"
    assert started.record.attributes == {}
