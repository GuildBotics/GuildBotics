from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from guildbotics.app_api.events import EventBus
from guildbotics.app_api.models import (
    RuntimeStatus,
    RuntimeUnitStatus,
    SchedulerStartRequest,
)
from guildbotics.drivers import EventListenerRunner, TaskScheduler
from guildbotics.observability import new_id
from guildbotics.runtime import Context

RuntimeTarget = Literal["scheduler", "events"]
RuntimeState = Literal["starting", "running", "stopping", "stopped", "failed"]


class RuntimeLifecycleService:
    def __init__(
        self,
        *,
        event_bus: EventBus,
        context_factory: Callable[[], Context],
        default_routines_factory: Callable[[], list[str]],
        stop_timeout_seconds: float = 10.0,
    ) -> None:
        self._scheduler = SchedulerLifecycle(
            event_bus=event_bus,
            context_factory=context_factory,
            default_routines_factory=default_routines_factory,
            stop_timeout_seconds=stop_timeout_seconds,
        )
        self._events = EventListenerLifecycle(
            event_bus=event_bus,
            context_factory=context_factory,
            stop_timeout_seconds=stop_timeout_seconds,
        )

    def get_status(self) -> RuntimeStatus:
        return RuntimeStatus(
            scheduler=self._scheduler.get_status(),
            events=self._events.get_status(),
        )

    def start(self, request: SchedulerStartRequest) -> RuntimeStatus:
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
            routine_commands = (
                (request.routine_commands or self._scheduler.default_routines())
                if routine_source_enabled
                else []
            )
            self._scheduler.start(
                routine_commands=routine_commands,
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
        return self.get_status()

    def stop(self) -> RuntimeStatus:
        self._events.stop()
        self._scheduler.stop()
        return self.get_status()


class _RuntimeLifecycle:
    def __init__(
        self,
        *,
        target: RuntimeTarget,
        event_bus: EventBus,
        stop_timeout_seconds: float,
    ) -> None:
        self._target = target
        self._event_bus = event_bus
        self._stop_timeout_seconds = stop_timeout_seconds
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
        default_routines_factory: Callable[[], list[str]],
        stop_timeout_seconds: float,
    ) -> None:
        super().__init__(
            target="scheduler",
            event_bus=event_bus,
            stop_timeout_seconds=stop_timeout_seconds,
        )
        self._context_factory = context_factory
        self._default_routines_factory = default_routines_factory
        self._scheduler: TaskScheduler | None = None
        self._thread: threading.Thread | None = None

    def default_routines(self) -> list[str]:
        return self._default_routines_factory()

    def start(
        self,
        *,
        routine_commands: list[str],
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
                routine_commands,
                consecutive_error_limit=max_consecutive_errors,
                routine_interval_minutes=routine_interval_minutes,
                service_run_id=self._service_run_id,
                scheduled_source_enabled=scheduled_source_enabled,
                routine_source_enabled=routine_source_enabled,
                event_queue_source_enabled=event_queue_source_enabled,
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
                    "routine_commands": list(routine_commands),
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

    def stop(self) -> RuntimeUnitStatus:
        with self._lock:
            self._refresh_locked()
            if not self._is_active_locked():
                return self._status.model_copy()
            scheduler = self._scheduler
            thread = self._thread
            self._transition_locked("stopping", running=True)

        if scheduler is not None:
            scheduler.shutdown(graceful=True, timeout=self._stop_timeout_seconds)
        if thread is not None:
            thread.join(timeout=self._stop_timeout_seconds)

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
            return self._transition_locked("stopped", running=False).model_copy()

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
    ) -> None:
        super().__init__(
            target="events",
            event_bus=event_bus,
            stop_timeout_seconds=stop_timeout_seconds,
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
                self._context_factory(), service_run_id=self._service_run_id
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
            return self._transition_locked("stopped", running=False).model_copy()

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
