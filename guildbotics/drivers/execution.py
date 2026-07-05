from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from guildbotics.observability import new_id

WorkSource = Literal["manual", "scheduled", "routine", "event_queue"]


class WorkRejectedError(RuntimeError):
    """Raised when new work is submitted while the runtime is draining."""


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
    ) -> Iterator[ActiveWork]:
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
                    "Runtime is stopping; new work is not accepted."
                )
            self._active[work.id] = _WorkEntry(work=work, cancel=cancel)
            self._condition.notify_all()
        try:
            yield work
        finally:
            with self._condition:
                self._active.pop(work.id, None)
                if not self._active and self._draining:
                    self._draining = False
                self._condition.notify_all()

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
