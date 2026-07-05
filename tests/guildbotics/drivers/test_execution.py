from __future__ import annotations

import threading

import pytest

from guildbotics.drivers.execution import ExecutionCoordinator, WorkRejectedError


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


def test_begin_drain_rejects_new_work_until_drained() -> None:
    coordinator = ExecutionCoordinator()
    release = threading.Event()
    thread = _start_tracked_work(coordinator, release)
    coordinator.begin_drain()

    with pytest.raises(WorkRejectedError):
        with coordinator.track_work(
            source="manual", person_id="alice", command="rejected"
        ):
            pass

    release.set()
    assert coordinator.wait_for_drain(timeout=5) is True
    thread.join(timeout=5)

    with coordinator.track_work(source="manual", person_id="alice", command="next"):
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

    # The drain window closed even though the stuck work never finished, so
    # the runtime accepts new work again instead of rejecting it forever.
    with coordinator.track_work(source="manual", person_id="alice", command="next"):
        pass

    release.set()
    thread.join(timeout=5)
