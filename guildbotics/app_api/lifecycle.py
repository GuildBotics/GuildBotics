from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from guildbotics.app_api.events import EventBus
from guildbotics.app_api.models import (
    RuntimeActiveWork,
    RuntimeStatus,
    RuntimeUnitStatus,
    SchedulerStartRequest,
)
from guildbotics.drivers import EventListenerRunner, TaskScheduler
from guildbotics.drivers.execution import ActiveWork, ExecutionCoordinator
from guildbotics.observability import new_id
from guildbotics.runtime import Context
from guildbotics.runtime.service_lock import ServiceLock

RuntimeTarget = Literal["scheduler", "events"]
RuntimeState = Literal["starting", "running", "stopping", "stopped", "failed"]
RuntimeStateCallback = Callable[[RuntimeTarget, bool], None]


class RuntimeLifecycleService:
    def __init__(
        self,
        *,
        event_bus: EventBus,
        context_factory: Callable[[], Context],
        stop_timeout_seconds: float = 10.0,
        execution_coordinator: ExecutionCoordinator | None = None,
        service_lock: ServiceLock | None = None,
    ) -> None:
        self._execution = execution_coordinator or ExecutionCoordinator()
        self._service_lock = service_lock or ServiceLock()
        self._start_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_targets: set[RuntimeTarget] = set()
        self._start_in_progress = False
        self._scheduler = SchedulerLifecycle(
            event_bus=event_bus,
            context_factory=context_factory,
            stop_timeout_seconds=stop_timeout_seconds,
            execution_coordinator=self._execution,
            state_callback=self._runtime_state_changed,
        )
        self._events = EventListenerLifecycle(
            event_bus=event_bus,
            context_factory=context_factory,
            stop_timeout_seconds=stop_timeout_seconds,
            state_callback=self._runtime_state_changed,
        )

    def get_status(self) -> RuntimeStatus:
        return RuntimeStatus(
            scheduler=self._scheduler.get_status(),
            events=self._events.get_status(),
            active_works=[
                _active_work_model(work) for work in self._execution.snapshot()
            ],
        )

    def start(self, request: SchedulerStartRequest) -> RuntimeStatus:
        with self._start_lock:
            return self._start(request)

    def _start(self, request: SchedulerStartRequest) -> RuntimeStatus:
        if not self._service_lock.locked:
            self._service_lock.acquire(owner="desktop", workspace=Path.cwd())

        with self._state_lock:
            self._start_in_progress = True

        try:
            sources = request.sources
            scheduled_source_enabled = sources.scheduled
            routine_source_enabled = sources.routine
            event_queue_source_enabled = sources.event_queue
            scheduler_was_running = self._scheduler.get_status().running
            if (
                scheduled_source_enabled
                or routine_source_enabled
                or event_queue_source_enabled
            ):
                self._scheduler.start(
                    max_consecutive_errors=request.max_consecutive_errors,
                    routine_interval_minutes=request.routine_interval_minutes,
                    scheduled_source_enabled=scheduled_source_enabled,
                    routine_source_enabled=routine_source_enabled,
                    event_queue_source_enabled=event_queue_source_enabled,
                )
            if event_queue_source_enabled:
                try:
                    self._events.start()
                except Exception:
                    if not scheduler_was_running:
                        self._scheduler.stop()
                    raise
        finally:
            self._finish_start()
        return self.get_status()

    def stop(self, *, force: bool = False) -> RuntimeStatus:
        # begin_drain rejects new work from every source (including manual
        # commands) and, on force, cancels the in-flight work. Scheduler
        # workers that race into the drain treat the rejection as shutdown
        # (see TaskScheduler._run_work), not as a command error.
        self._execution.begin_drain(force=force)
        self._events.stop()
        self._scheduler.stop(force=force)
        self._execution.wait_for_drain(
            timeout=None if not force else self._scheduler.stop_timeout_seconds
        )
        return self.get_status()

    def _runtime_state_changed(self, target: RuntimeTarget, running: bool) -> None:
        with self._state_lock:
            if running:
                self._active_targets.add(target)
            else:
                self._active_targets.discard(target)
            should_release = not self._start_in_progress and not self._active_targets
        if should_release:
            self._service_lock.release()

    def _finish_start(self) -> None:
        with self._state_lock:
            self._start_in_progress = False
            should_release = not self._active_targets
        if should_release:
            self._service_lock.release()


class _RuntimeLifecycle:
    def __init__(
        self,
        *,
        target: RuntimeTarget,
        event_bus: EventBus,
        stop_timeout_seconds: float,
        state_callback: RuntimeStateCallback,
    ) -> None:
        self._target = target
        self._event_bus = event_bus
        self._stop_timeout_seconds = stop_timeout_seconds
        self._state_callback = state_callback
        self._lock = threading.Lock()
        self._service_run_id: str | None = None
        self._status = RuntimeUnitStatus(
            target=target,
            state="stopped",
            running=False,
        )

    def get_status(self) -> RuntimeUnitStatus:
        with self._lock:
            self._refresh_locked()
            return self._status.model_copy()

    def _is_active_locked(self) -> bool:
        return self._status.state in {"starting", "running", "stopping"}

    def _transition_locked(
        self,
        state: RuntimeState,
        *,
        running: bool,
        error: str | None = None,
    ) -> RuntimeUnitStatus:
        now = _timestamp()
        started_at = self._status.started_at
        stopped_at = self._status.stopped_at

        if state in {"starting", "running"}:
            started_at = started_at or now
            stopped_at = None
            error = None
        elif state in {"stopped", "failed"}:
            stopped_at = now
            if state == "stopped":
                error = None

        self._status = self._status.model_copy(
            update={
                "target": self._target,
                "state": state,
                "running": running,
                "started_at": started_at,
                "stopped_at": stopped_at,
                "error": error,
            }
        )
        self._event_bus.publish_event(
            f"{self._target}.{state}",
            self._status.model_dump(),
            source=self._target,
            attributes=(
                {"service_run_id": self._service_run_id}
                if self._service_run_id
                else None
            ),
        )
        self._state_callback(self._target, running)
        return self._status

    def _update_metadata_locked(self, values: dict[str, Any]) -> None:
        if values:
            self._status = self._status.model_copy(update=values)

    def _refresh_locked(self) -> None:
        return

    def _mark_failed(self, exc: Exception) -> None:
        with self._lock:
            self._transition_locked("failed", running=False, error=str(exc))


class SchedulerLifecycle(_RuntimeLifecycle):
    def __init__(
        self,
        *,
        event_bus: EventBus,
        context_factory: Callable[[], Context],
        stop_timeout_seconds: float,
        execution_coordinator: ExecutionCoordinator,
        state_callback: RuntimeStateCallback,
    ) -> None:
        super().__init__(
            target="scheduler",
            event_bus=event_bus,
            stop_timeout_seconds=stop_timeout_seconds,
            state_callback=state_callback,
        )
        self._context_factory = context_factory
        self._execution = execution_coordinator
        self._scheduler: TaskScheduler | None = None
        self._thread: threading.Thread | None = None

    @property
    def stop_timeout_seconds(self) -> float:
        return self._stop_timeout_seconds

    def start(
        self,
        *,
        max_consecutive_errors: int,
        routine_interval_minutes: int,
        scheduled_source_enabled: bool = True,
        routine_source_enabled: bool = True,
        event_queue_source_enabled: bool = True,
    ) -> RuntimeUnitStatus:
        with self._lock:
            self._refresh_locked()
            if self._is_active_locked():
                return self._status.model_copy()
            self._service_run_id = new_id()
            self._transition_locked("starting", running=True)

        try:
            scheduler = TaskScheduler(
                self._context_factory(),
                consecutive_error_limit=max_consecutive_errors,
                routine_interval_minutes=routine_interval_minutes,
                service_run_id=self._service_run_id,
                scheduled_source_enabled=scheduled_source_enabled,
                routine_source_enabled=routine_source_enabled,
                event_queue_source_enabled=event_queue_source_enabled,
                execution_coordinator=self._execution,
            )
        except Exception as exc:
            self._mark_failed(exc)
            raise

        thread = threading.Thread(
            target=self._run_scheduler,
            args=(scheduler,),
            name="guildbotics-app-api-scheduler",
            daemon=True,
        )
        with self._lock:
            self._scheduler = scheduler
            self._thread = thread
            self._update_metadata_locked(
                {
                    "max_consecutive_errors": max_consecutive_errors,
                    "routine_interval_minutes": routine_interval_minutes,
                    "scheduled_source_enabled": scheduled_source_enabled,
                    "routine_source_enabled": routine_source_enabled,
                    "event_queue_source_enabled": event_queue_source_enabled,
                    **_runtime_summary(scheduler),
                }
            )
            status = self._transition_locked("running", running=True)
        thread.start()
        return status.model_copy()

    def stop(self, *, force: bool = False) -> RuntimeUnitStatus:
        with self._lock:
            self._refresh_locked()
            if not self._is_active_locked():
                return self._status.model_copy()
            scheduler = self._scheduler
            thread = self._thread
            # A concurrent stop (graceful stop escalated by a force stop) may
            # already be in "stopping"; do not publish the transition twice.
            if self._status.state != "stopping":
                self._transition_locked("stopping", running=True)

        if scheduler is not None:
            scheduler.shutdown(
                graceful=not force,
                timeout=None if not force else self._stop_timeout_seconds,
            )
        if thread is not None:
            thread.join(timeout=None if not force else self._stop_timeout_seconds)

        with self._lock:
            if thread is not None and thread.is_alive():
                self._update_metadata_locked(_runtime_summary(scheduler))
                return self._transition_locked(
                    "failed",
                    running=True,
                    error="Scheduler did not stop before timeout.",
                ).model_copy()
            self._update_metadata_locked({"worker_count": 0})
            self._scheduler = None
            self._thread = None
            if self._status.state != "stopped":
                return self._transition_locked("stopped", running=False).model_copy()
            return self._status.model_copy()

    def _run_scheduler(self, scheduler: TaskScheduler) -> None:
        try:
            scheduler.start()
        except Exception as exc:
            self._event_bus.publish_event(
                "scheduler.failed",
                {"error": str(exc), "traceback": traceback.format_exc()},
            )
            with self._lock:
                self._transition_locked("failed", running=False, error=str(exc))
        else:
            with self._lock:
                if self._status.state != "stopping":
                    self._scheduler = None
                    self._thread = None
                    self._transition_locked("stopped", running=False)

    def _refresh_locked(self) -> None:
        if (
            self._thread is not None
            and not self._thread.is_alive()
            and (self._status.state != "failed" or self._status.running)
        ):
            self._update_metadata_locked({"worker_count": 0})
            self._scheduler = None
            self._thread = None
            if self._status.state != "stopped":
                self._transition_locked("stopped", running=False)
        elif self._scheduler is not None:
            self._update_metadata_locked(_runtime_summary(self._scheduler))


class EventListenerLifecycle(_RuntimeLifecycle):
    def __init__(
        self,
        *,
        event_bus: EventBus,
        context_factory: Callable[[], Context],
        stop_timeout_seconds: float,
        state_callback: RuntimeStateCallback,
    ) -> None:
        super().__init__(
            target="events",
            event_bus=event_bus,
            stop_timeout_seconds=stop_timeout_seconds,
            state_callback=state_callback,
        )
        self._context_factory = context_factory
        self._runner: EventListenerRunner | None = None

    def start(self) -> RuntimeUnitStatus:
        with self._lock:
            self._refresh_locked()
            if self._is_active_locked():
                return self._status.model_copy()
            self._service_run_id = new_id()
            self._transition_locked("starting", running=True)

        try:
            runner = EventListenerRunner(
                self._context_factory(),
                service_run_id=self._service_run_id,
                on_stopped=lambda: self._runner_stopped(runner),
            )
            runner.start()
        except Exception as exc:
            self._mark_failed(exc)
            raise

        with self._lock:
            self._runner = runner
            self._update_metadata_locked(_runtime_summary(runner))
            if runner.is_alive():
                status = self._transition_locked("running", running=True)
            else:
                status = self._transition_locked("stopped", running=False)
            return status.model_copy()

    def stop(self) -> RuntimeUnitStatus:
        with self._lock:
            self._refresh_locked()
            if not self._is_active_locked():
                return self._status.model_copy()
            runner = self._runner
            self._transition_locked("stopping", running=True)

        if runner is not None:
            runner.stop()
            runner.join(timeout=self._stop_timeout_seconds)

        with self._lock:
            if runner is not None and runner.is_alive():
                self._update_metadata_locked(_runtime_summary(runner))
                return self._transition_locked(
                    "failed",
                    running=True,
                    error="Event listener runner did not stop before timeout.",
                ).model_copy()
            if runner is not None:
                self._update_metadata_locked(_runtime_summary(runner))
            self._runner = None
            if self._status.state != "stopped":
                return self._transition_locked("stopped", running=False).model_copy()
            return self._status.model_copy()

    def _runner_stopped(self, runner: EventListenerRunner) -> None:
        with self._lock:
            if self._runner is not runner:
                return
            self._update_metadata_locked(_runtime_summary(self._runner))
            self._runner = None
            if self._status.state not in {"stopping", "stopped"}:
                self._transition_locked("stopped", running=False)

    def _refresh_locked(self) -> None:
        if (
            self._runner is not None
            and not self._runner.is_alive()
            and (self._status.state != "failed" or self._status.running)
        ):
            self._update_metadata_locked(_runtime_summary(self._runner))
            self._runner = None
            if self._status.state != "stopped":
                self._transition_locked("stopped", running=False)
        elif self._runner is not None:
            self._update_metadata_locked(_runtime_summary(self._runner))


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat()


def _active_work_model(work: ActiveWork) -> RuntimeActiveWork:
    return RuntimeActiveWork(
        id=work.id,
        source=work.source,
        person_id=work.person_id,
        command=work.command,
        started_at=work.started_at,
    )


def _runtime_summary(runtime: object | None) -> dict[str, Any]:
    if runtime is None:
        return {}
    get_status_summary = getattr(runtime, "get_status_summary", None)
    if not callable(get_status_summary):
        return {}
    summary = get_status_summary()
    if isinstance(summary, dict):
        return summary
    return {}
