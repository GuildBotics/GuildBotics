"""Context-local observers for raw diagnostics records.

The observability layer owns record persistence, while runtime consumers may
need the same record at the moment it is written. Keeping this hook here lets
observability notify those consumers without depending on runtime or the app
API's presentation models.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import Any

DiagnosticsRecordObserver = Callable[[dict[str, Any]], None]

_observers: ContextVar[tuple[DiagnosticsRecordObserver, ...]] = ContextVar(
    "guildbotics_diagnostics_record_observers", default=()
)


@contextmanager
def diagnostics_record_scope(
    observer: DiagnosticsRecordObserver,
) -> Iterator[None]:
    """Observe records written during the current execution context."""
    token = _observers.set((*_observers.get(), observer))
    try:
        yield
    finally:
        _observers.reset(token)


def notify_diagnostics_record(item: dict[str, Any]) -> None:
    """Notify active observers without making live display execution-fatal."""
    for observer in _observers.get():
        with suppress(Exception):
            observer(item)
