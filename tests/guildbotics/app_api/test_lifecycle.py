"""Unit tests for ``guildbotics.app_api.lifecycle``.

The real scheduler / event-listener runners and the context factory are replaced
with light-weight fakes so that no real scheduler loop or external I/O runs. The
tests assert concrete observable behaviour: published event sequences, status
fields (``state`` / ``running`` / metadata), thread non-duplication and stop
ordering.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any, ClassVar

import pytest

from guildbotics.app_api import lifecycle
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.models import SchedulerStartRequest

EXPECTED_WORKER_COUNT = 2
EXPECTED_MEMBER_COUNT = 2
EXPECTED_MAX_ERRORS = 5
EXPECTED_INTERVAL = 7
REFRESHED_WORKER_COUNT = 3


def _wait_until(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _event_types(bus: EventBus) -> list[str]:
    return [item["type"] for item in bus.snapshot_events()]


def _events_for(bus: EventBus, target: str) -> list[str]:
    return [t for t in _event_types(bus) if t.startswith(f"{target}.")]


class FakeScheduler:
    """Stand-in for ``TaskScheduler`` driven entirely by test signals."""

    instances: ClassVar[list[FakeScheduler]] = []

    def __init__(
        self,
        context: Any,
        *,
        consecutive_error_limit: int,
        routine_interval_minutes: int,
        service_run_id: str | None = None,
        scheduled_source_enabled: bool = True,
        routine_source_enabled: bool = True,
        event_queue_source_enabled: bool = True,
        execution_coordinator: Any = None,
    ) -> None:
        self.context = context
        self.consecutive_error_limit = consecutive_error_limit
        self.routine_interval_minutes = routine_interval_minutes
        self.service_run_id = service_run_id
        self.scheduled_source_enabled = scheduled_source_enabled
        self.routine_source_enabled = routine_source_enabled
        self.event_queue_source_enabled = event_queue_source_enabled
        self._stop = threading.Event()
        self.shutdown_calls: list[dict[str, Any]] = []
        self.start_error: Exception | None = None
        self.summary: dict[str, Any] = {
            "active_member_count": 2,
            "worker_count": 2,
            "routine_interval_minutes": routine_interval_minutes,
            "scheduled_source_enabled": scheduled_source_enabled,
            "routine_source_enabled": routine_source_enabled,
            "event_queue_source_enabled": event_queue_source_enabled,
        }
        self.block_shutdown = False
        self.block_graceful_shutdown = False
        FakeScheduler.instances.append(self)

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        # Block until shutdown is requested, mimicking a running loop.
        self._stop.wait()

    def shutdown(self, graceful: bool = True, timeout: float | None = None) -> None:
        self.shutdown_calls.append({"graceful": graceful, "timeout": timeout})
        if self.block_shutdown:
            return
        if self.block_graceful_shutdown and graceful:
            # Mimic a worker busy with in-flight work: the graceful shutdown
            # keeps waiting until a forceful shutdown cancels the work.
            return
        self._stop.set()

    def get_status_summary(self) -> dict[str, Any]:
        return dict(self.summary)


class FakeRunner:
    """Stand-in for ``EventListenerRunner``."""

    instances: ClassVar[list[FakeRunner]] = []

    def __init__(
        self,
        context: Any,
        service_run_id: str | None = None,
        on_stopped: Any = None,
    ) -> None:
        self.context = context
        self.service_run_id = service_run_id
        self.on_stopped = on_stopped
        self._alive = True
        self.start_calls = 0
        self.stop_calls = 0
        self.join_calls: list[float | None] = []
        self.block_stop = False
        self.summary: dict[str, Any] = {
            "subscription_count": 1,
            "listener_count": 1,
            "cycle_count": 0,
            "cycle_failure_count": 0,
            "events_drained_count": 0,
        }
        FakeRunner.instances.append(self)

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1
        if not self.block_stop:
            self._alive = False

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append(timeout)

    def is_alive(self) -> bool:
        return self._alive

    def get_status_summary(self) -> dict[str, Any]:
        return dict(self.summary)

    def finish(self) -> None:
        self._alive = False
        if self.on_stopped is not None:
            self.on_stopped()


class FakeServiceLock:
    instances: ClassVar[list[FakeServiceLock]] = []

    def __init__(self) -> None:
        self.locked = False
        self.acquire_calls: list[dict[str, Any]] = []
        self.release_calls = 0
        FakeServiceLock.instances.append(self)

    def acquire(self, *, owner: str, workspace: Any) -> None:
        self.acquire_calls.append({"owner": owner, "workspace": workspace})
        self.locked = True

    def release(self) -> None:
        if not self.locked:
            return
        self.release_calls += 1
        self.locked = False


@pytest.fixture
def context_factory() -> Any:
    sentinel = object()

    def factory() -> Any:
        return sentinel

    factory.sentinel = sentinel  # type: ignore[attr-defined]
    return factory


@pytest.fixture(autouse=True)
def patch_runtimes(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeScheduler.instances = []
    FakeRunner.instances = []
    FakeServiceLock.instances = []
    monkeypatch.setattr(lifecycle, "TaskScheduler", FakeScheduler)
    monkeypatch.setattr(lifecycle, "EventListenerRunner", FakeRunner)
    monkeypatch.setattr(lifecycle, "ServiceLock", FakeServiceLock)


def _make_service(
    context_factory: Any,
    *,
    stop_timeout_seconds: float = 2.0,
) -> tuple[lifecycle.RuntimeLifecycleService, EventBus]:
    bus = EventBus()
    service = lifecycle.RuntimeLifecycleService(
        event_bus=bus,
        context_factory=context_factory,
        stop_timeout_seconds=stop_timeout_seconds,
    )
    return service, bus


def _stop_quietly(service: lifecycle.RuntimeLifecycleService) -> None:
    with contextlib.suppress(Exception):
        service.stop()


# --------------------------------------------------------------------------- #
# Start event sequencing
# --------------------------------------------------------------------------- #


def test_scheduler_start_publishes_starting_then_running(context_factory: Any) -> None:
    service, bus = _make_service(context_factory)
    try:
        status = service.start(
            SchedulerStartRequest(
                sources={"scheduled": True, "routine": True, "event_queue": False}
            )
        )

        assert _events_for(bus, "scheduler") == [
            "scheduler.starting",
            "scheduler.running",
        ]
        assert _events_for(bus, "events") == []
        assert status.scheduler.state == "running"
        assert status.scheduler.running is True
        assert status.events.state == "stopped"
    finally:
        _stop_quietly(service)


def test_events_start_publishes_starting_then_running(context_factory: Any) -> None:
    service, bus = _make_service(context_factory)
    try:
        status = service.start(
            SchedulerStartRequest(
                sources={"scheduled": False, "routine": False, "event_queue": True}
            )
        )

        assert _events_for(bus, "events") == ["events.starting", "events.running"]
        assert _events_for(bus, "scheduler") == [
            "scheduler.starting",
            "scheduler.running",
        ]
        assert status.events.state == "running"
        assert status.events.running is True
        assert status.scheduler.event_queue_source_enabled is True
        assert status.scheduler.scheduled_source_enabled is False
        assert status.scheduler.routine_source_enabled is False
    finally:
        _stop_quietly(service)


def test_context_factory_is_used_for_runtimes(context_factory: Any) -> None:
    service, _bus = _make_service(context_factory)
    try:
        service.start(SchedulerStartRequest())
        assert FakeScheduler.instances[0].context is context_factory.sentinel
        assert FakeRunner.instances[0].context is context_factory.sentinel
    finally:
        _stop_quietly(service)


# --------------------------------------------------------------------------- #
# Target selection
# --------------------------------------------------------------------------- #


def test_only_scheduler_starts_only_scheduler(context_factory: Any) -> None:
    service, _bus = _make_service(context_factory)
    try:
        service.start(
            SchedulerStartRequest(
                sources={"scheduled": True, "routine": True, "event_queue": False}
            )
        )
        assert len(FakeScheduler.instances) == 1
        assert FakeRunner.instances == []
    finally:
        _stop_quietly(service)


def test_only_events_starts_event_listener_and_event_queue_worker(
    context_factory: Any,
) -> None:
    service, _bus = _make_service(context_factory)
    try:
        service.start(
            SchedulerStartRequest(
                sources={"scheduled": False, "routine": False, "event_queue": True}
            )
        )
        assert len(FakeScheduler.instances) == 1
        worker = FakeScheduler.instances[0]
        assert worker.scheduled_source_enabled is False
        assert worker.routine_source_enabled is False
        assert worker.event_queue_source_enabled is True
        assert len(FakeRunner.instances) == 1
    finally:
        _stop_quietly(service)


def test_both_targets_start_when_only_is_none(context_factory: Any) -> None:
    service, bus = _make_service(context_factory)
    try:
        status = service.start(SchedulerStartRequest())
        assert len(FakeScheduler.instances) == 1
        assert len(FakeRunner.instances) == 1
        assert status.scheduler.state == "running"
        assert status.events.state == "running"
        # Selection order starts the member worker before the event listener so
        # queued events have a consumer as soon as they are received.
        assert _event_types(bus) == [
            "scheduler.starting",
            "scheduler.running",
            "events.starting",
            "events.running",
        ]
    finally:
        _stop_quietly(service)


def test_service_lock_is_shared_by_scheduler_and_events(context_factory: Any) -> None:
    service, _bus = _make_service(context_factory)
    service.start(
        SchedulerStartRequest(
            sources={"scheduled": True, "routine": True, "event_queue": False}
        )
    )
    service.start(
        SchedulerStartRequest(
            sources={"scheduled": False, "routine": False, "event_queue": True}
        )
    )

    service_lock = FakeServiceLock.instances[0]
    assert len(service_lock.acquire_calls) == 1
    assert service_lock.acquire_calls[0]["owner"] == "desktop"

    service.stop()

    assert service_lock.release_calls == 1
    assert service_lock.locked is False


def test_status_poll_does_not_release_lock_during_start(context_factory: Any) -> None:
    service, _bus = _make_service(context_factory)
    entered_start = threading.Event()
    continue_start = threading.Event()
    original_start = service._scheduler.start

    def blocked_start(**kwargs: Any) -> Any:
        entered_start.set()
        assert continue_start.wait(2.0)
        return original_start(**kwargs)

    service._scheduler.start = blocked_start  # type: ignore[method-assign]
    start_thread = threading.Thread(
        target=service.start,
        args=(
            SchedulerStartRequest(
                sources={"scheduled": True, "routine": True, "event_queue": False}
            ),
        ),
    )
    start_thread.start()
    try:
        assert entered_start.wait(2.0)
        assert service.get_status().scheduler.running is False
        assert FakeServiceLock.instances[0].locked is True
    finally:
        continue_start.set()
        start_thread.join(timeout=2.0)
        _stop_quietly(service)
    assert not start_thread.is_alive()


def test_event_runner_natural_stop_releases_lock_without_status_poll(
    context_factory: Any,
) -> None:
    service, _bus = _make_service(context_factory)
    service.start(
        SchedulerStartRequest(
            sources={"scheduled": False, "routine": False, "event_queue": True}
        )
    )
    # Stop the scheduler first so the event listener is the final active target.
    service._scheduler.stop()

    FakeRunner.instances[0].finish()

    assert FakeServiceLock.instances[0].locked is False


# --------------------------------------------------------------------------- #
# Non-duplication on re-start
# --------------------------------------------------------------------------- #


def test_restart_scheduler_does_not_spawn_duplicate(context_factory: Any) -> None:
    service, bus = _make_service(context_factory)
    try:
        service.start(
            SchedulerStartRequest(
                sources={"scheduled": True, "routine": True, "event_queue": False}
            )
        )
        first_count = len(FakeScheduler.instances)

        service.start(
            SchedulerStartRequest(
                sources={"scheduled": True, "routine": True, "event_queue": False}
            )
        )

        assert len(FakeScheduler.instances) == first_count == 1
        # No additional starting/running events from the second call.
        assert _events_for(bus, "scheduler") == [
            "scheduler.starting",
            "scheduler.running",
        ]
    finally:
        _stop_quietly(service)


def test_restart_events_does_not_spawn_duplicate(context_factory: Any) -> None:
    service, bus = _make_service(context_factory)
    try:
        service.start(
            SchedulerStartRequest(
                sources={"scheduled": False, "routine": False, "event_queue": True}
            )
        )
        service.start(
            SchedulerStartRequest(
                sources={"scheduled": False, "routine": False, "event_queue": True}
            )
        )

        assert len(FakeScheduler.instances) == 1
        assert len(FakeRunner.instances) == 1
        assert _events_for(bus, "events") == ["events.starting", "events.running"]
        assert _events_for(bus, "scheduler") == [
            "scheduler.starting",
            "scheduler.running",
        ]
    finally:
        _stop_quietly(service)


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #


def test_scheduler_metadata_reflects_request_and_summary(context_factory: Any) -> None:
    service, _bus = _make_service(context_factory)
    try:
        service.start(
            SchedulerStartRequest(
                sources={"scheduled": True, "routine": True, "event_queue": False},
                max_consecutive_errors=5,
                routine_interval_minutes=7,
            )
        )
        status = service.get_status().scheduler
        assert status.max_consecutive_errors == EXPECTED_MAX_ERRORS
        assert status.routine_interval_minutes == EXPECTED_INTERVAL
        assert status.active_member_count == EXPECTED_MEMBER_COUNT
        assert status.worker_count == EXPECTED_WORKER_COUNT
        assert status.scheduled_source_enabled is True
        assert status.routine_source_enabled is True
        assert status.event_queue_source_enabled is False
    finally:
        _stop_quietly(service)


def test_events_metadata_reflects_summary(context_factory: Any) -> None:
    service, _bus = _make_service(context_factory)
    try:
        service.start(
            SchedulerStartRequest(
                sources={"scheduled": False, "routine": False, "event_queue": True}
            )
        )
        status = service.get_status().events
        assert status.subscription_count == 1
        assert status.listener_count == 1
        assert status.cycle_count == 0
    finally:
        _stop_quietly(service)


def test_get_status_refreshes_scheduler_cycle_metadata(context_factory: Any) -> None:
    service, _bus = _make_service(context_factory)
    try:
        service.start(
            SchedulerStartRequest(
                sources={"scheduled": True, "routine": True, "event_queue": False}
            )
        )
        FakeScheduler.instances[0].summary["worker_count"] = REFRESHED_WORKER_COUNT
        # get_status -> _refresh_locked re-reads the live summary.
        assert service.get_status().scheduler.worker_count == REFRESHED_WORKER_COUNT
    finally:
        _stop_quietly(service)


# --------------------------------------------------------------------------- #
# Stop ordering and success
# --------------------------------------------------------------------------- #


def test_stop_stops_events_before_scheduler(context_factory: Any) -> None:
    service, bus = _make_service(context_factory)
    service.start(SchedulerStartRequest())

    order: list[str] = []
    runner = FakeRunner.instances[0]
    scheduler = FakeScheduler.instances[0]
    original_runner_stop = runner.stop
    original_scheduler_shutdown = scheduler.shutdown

    def tracked_runner_stop() -> None:
        order.append("events")
        original_runner_stop()

    def tracked_scheduler_shutdown(graceful: bool = True, timeout: Any = None) -> None:
        order.append("scheduler")
        original_scheduler_shutdown(graceful=graceful, timeout=timeout)

    runner.stop = tracked_runner_stop  # type: ignore[method-assign]
    scheduler.shutdown = tracked_scheduler_shutdown  # type: ignore[method-assign]

    status = service.stop()

    assert order == ["events", "scheduler"]
    assert status.events.state == "stopped"
    assert status.events.running is False
    assert status.scheduler.state == "stopped"
    assert status.scheduler.running is False
    # worker_count is zeroed out once the scheduler has stopped.
    assert status.scheduler.worker_count == 0
    assert _events_for(bus, "scheduler")[-2:] == [
        "scheduler.stopping",
        "scheduler.stopped",
    ]
    assert _events_for(bus, "events")[-2:] == ["events.stopping", "events.stopped"]


def test_graceful_scheduler_shutdown_waits_without_timeout(
    context_factory: Any,
) -> None:
    service, _bus = _make_service(context_factory, stop_timeout_seconds=4.5)
    service.start(
        SchedulerStartRequest(
            sources={"scheduled": True, "routine": True, "event_queue": False}
        )
    )
    scheduler = FakeScheduler.instances[0]

    service.stop()

    assert scheduler.shutdown_calls == [{"graceful": True, "timeout": None}]


def test_force_scheduler_shutdown_uses_stop_timeout(context_factory: Any) -> None:
    service, _bus = _make_service(context_factory, stop_timeout_seconds=4.5)
    service.start(
        SchedulerStartRequest(
            sources={"scheduled": True, "routine": True, "event_queue": False}
        )
    )
    scheduler = FakeScheduler.instances[0]

    service.stop(force=True)

    assert scheduler.shutdown_calls == [{"graceful": False, "timeout": 4.5}]


def test_force_stop_during_graceful_stop_publishes_single_stop_transition(
    context_factory: Any,
) -> None:
    service, bus = _make_service(context_factory)
    service.start(
        SchedulerStartRequest(
            sources={"scheduled": True, "routine": True, "event_queue": False}
        )
    )
    scheduler = FakeScheduler.instances[0]
    scheduler.block_graceful_shutdown = True

    graceful = threading.Thread(target=service.stop)
    graceful.start()
    try:
        assert _wait_until(lambda: service.get_status().scheduler.state == "stopping")

        status = service.stop(force=True)
    finally:
        graceful.join(timeout=2.0)
    assert not graceful.is_alive()
    assert status.scheduler.state == "stopped"

    events = _events_for(bus, "scheduler")
    assert events.count("scheduler.stopping") == 1
    assert events.count("scheduler.stopped") == 1


# --------------------------------------------------------------------------- #
# Stop timeout -> failed
# --------------------------------------------------------------------------- #


def test_scheduler_stop_timeout_marks_failed_with_running_true(
    context_factory: Any,
) -> None:
    service, bus = _make_service(context_factory, stop_timeout_seconds=0.1)
    service.start(
        SchedulerStartRequest(
            sources={"scheduled": True, "routine": True, "event_queue": False}
        )
    )
    scheduler = FakeScheduler.instances[0]
    # Prevent the scheduler thread from ever terminating.
    scheduler.block_shutdown = True

    try:
        status = service.stop(force=True)
        assert status.scheduler.state == "failed"
        assert status.scheduler.running is True
        assert status.scheduler.error == "Scheduler did not stop before timeout."
        assert "scheduler.failed" in _events_for(bus, "scheduler")
        assert FakeServiceLock.instances[0].locked is True
    finally:
        scheduler._stop.set()


def test_events_stop_timeout_marks_failed_with_running_true(
    context_factory: Any,
) -> None:
    service, bus = _make_service(context_factory, stop_timeout_seconds=0.1)
    service.start(
        SchedulerStartRequest(
            sources={"scheduled": False, "routine": False, "event_queue": True}
        )
    )
    runner = FakeRunner.instances[0]
    # Runner refuses to stop and stays alive past the join timeout.
    runner.block_stop = True

    status = service.stop()

    assert status.events.state == "failed"
    assert status.events.running is True
    assert status.events.error == "Event listener runner did not stop before timeout."
    assert "events.failed" in _events_for(bus, "events")
    assert runner.join_calls == [0.1]


# --------------------------------------------------------------------------- #
# context_factory failure
# --------------------------------------------------------------------------- #


def test_scheduler_start_context_factory_exception_marks_failed() -> None:
    bus = EventBus()

    def boom() -> Any:
        raise RuntimeError("no context")

    service = lifecycle.RuntimeLifecycleService(
        event_bus=bus,
        context_factory=boom,
    )

    with pytest.raises(RuntimeError, match="no context"):
        service.start(
            SchedulerStartRequest(
                sources={"scheduled": True, "routine": True, "event_queue": False}
            )
        )

    status = service.get_status().scheduler
    assert status.state == "failed"
    assert status.running is False
    assert status.error == "no context"
    assert _events_for(bus, "scheduler") == [
        "scheduler.starting",
        "scheduler.failed",
    ]
    assert FakeServiceLock.instances[0].locked is False
    assert FakeServiceLock.instances[0].release_calls == 1


def test_events_start_context_factory_exception_marks_member_worker_failed() -> None:
    bus = EventBus()

    def boom() -> Any:
        raise RuntimeError("no events context")

    service = lifecycle.RuntimeLifecycleService(
        event_bus=bus,
        context_factory=boom,
    )

    with pytest.raises(RuntimeError, match="no events context"):
        service.start(
            SchedulerStartRequest(
                sources={"scheduled": False, "routine": False, "event_queue": True}
            )
        )

    status = service.get_status().scheduler
    assert status.state == "failed"
    assert status.running is False
    assert status.error == "no events context"
    assert _events_for(bus, "scheduler") == ["scheduler.starting", "scheduler.failed"]
    assert _events_for(bus, "events") == []


def test_events_start_failure_stops_new_member_worker(context_factory: Any) -> None:
    service, bus = _make_service(context_factory)
    original_start = FakeRunner.start

    def broken_start(self: FakeRunner) -> None:
        original_start(self)
        raise RuntimeError("socket rejected")

    FakeRunner.start = broken_start  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="socket rejected"):
            service.start(
                SchedulerStartRequest(
                    sources={"scheduled": False, "routine": False, "event_queue": True}
                )
            )
    finally:
        FakeRunner.start = original_start  # type: ignore[method-assign]
        _stop_quietly(service)

    assert len(FakeScheduler.instances) == 1
    assert FakeScheduler.instances[0].shutdown_calls == [
        {"graceful": True, "timeout": None}
    ]
    status = service.get_status()
    assert status.scheduler.state == "stopped"
    assert status.events.state == "failed"
    assert _events_for(bus, "scheduler")[-2:] == [
        "scheduler.stopping",
        "scheduler.stopped",
    ]
    assert _events_for(bus, "events") == ["events.starting", "events.failed"]


# --------------------------------------------------------------------------- #
# Exception inside the scheduler thread
# --------------------------------------------------------------------------- #


def test_scheduler_thread_exception_reflected_in_failed_event_and_status(
    context_factory: Any,
) -> None:
    service, bus = _make_service(context_factory)
    bus_before = len(bus.snapshot_events())

    # Patch the next constructed scheduler to raise inside start().
    original_init = FakeScheduler.__init__

    def patched_init(self: FakeScheduler, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.start_error = RuntimeError("loop exploded")

    FakeScheduler.__init__ = patched_init  # type: ignore[method-assign]
    try:
        service.start(
            SchedulerStartRequest(
                sources={"scheduled": True, "routine": True, "event_queue": False}
            )
        )
        # Wait on the emitted event, without polling status as a release trigger.
        assert _wait_until(lambda: "scheduler.failed" in _events_for(bus, "scheduler"))
    finally:
        FakeScheduler.__init__ = original_init  # type: ignore[method-assign]

    status = service.get_status().scheduler
    assert status.state == "failed"
    assert status.running is False
    assert status.error == "loop exploded"
    assert FakeServiceLock.instances[0].locked is False

    failed_events = [
        item
        for item in bus.snapshot_events()[bus_before:]
        if item["type"] == "scheduler.failed"
    ]
    # The worker publishes a dedicated failure event carrying the traceback.
    assert any("traceback" in item["payload"] for item in failed_events)
    assert any(
        item["payload"].get("error") == "loop exploded" for item in failed_events
    )


# --------------------------------------------------------------------------- #
# Natural thread termination -> status refresh
# --------------------------------------------------------------------------- #


def test_status_refresh_after_thread_terminates_naturally(
    context_factory: Any,
) -> None:
    service, bus = _make_service(context_factory)
    service.start(
        SchedulerStartRequest(
            sources={"scheduled": True, "routine": True, "event_queue": False}
        )
    )
    scheduler = FakeScheduler.instances[0]

    # Let the scheduler loop finish on its own (no stop() call).
    scheduler._stop.set()
    assert _wait_until(lambda: service.get_status().scheduler.state == "stopped")

    status = service.get_status().scheduler
    assert status.state == "stopped"
    assert status.running is False
    # Natural completion publishes a stopped event from the worker thread.
    assert "scheduler.stopped" in _events_for(bus, "scheduler")
