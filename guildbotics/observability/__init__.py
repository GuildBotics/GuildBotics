"""Correlation core for runtime diagnostics.

Provides a single, contextvars-based source of truth for correlation IDs so
events, logs and prompt traces can be aggregated under the same execution unit.

Design (see ``docs/runtime_diagnostics_todo.ja.md`` 決定事項):

- ``trace_id`` identifies one bounded unit of work (one manual command run, one
  scheduler routine/scheduled command run, one received event-listener event,
  one diagnostics run). Trace roots are always "closing" units.
- ``span_id`` identifies an individual operation inside a trace (an LLM or CLI
  agent call). ``call_id`` pairs a span's request/response records.
- Grouping/continuity is expressed as ``attributes`` (``service_run_id``,
  ``slack.*``, ``github.*``) — never by overloading ``trace_id``.

``request_id`` is intentionally not part of this model; ``trace_id`` is the
single correlation id across the whole runtime.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

_current_trace: ContextVar[TraceContext | None] = ContextVar(
    "guildbotics_trace", default=None
)
_current_span: ContextVar[SpanContext | None] = ContextVar(
    "guildbotics_span", default=None
)


def new_id() -> str:
    """Return a fresh correlation id."""
    return uuid.uuid4().hex


@dataclass
class TraceContext:
    trace_id: str
    source: str
    person_id: str = ""
    command: str = ""
    workflow: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class SpanContext:
    span_id: str
    call_id: str
    parent_id: str | None
    name: str


def current_trace() -> TraceContext | None:
    return _current_trace.get()


def current_span() -> SpanContext | None:
    return _current_span.get()


@contextlib.contextmanager
def trace_scope(
    source: str,
    *,
    person_id: str = "",
    command: str = "",
    workflow: str = "",
    attributes: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
) -> Iterator[TraceContext]:
    """Bind a new trace as the current correlation context for the block.

    Entering a trace resets any inherited span so nested operations attach to
    this trace, not a leftover span from an outer scope.
    """
    ctx = TraceContext(
        trace_id=trace_id or new_id(),
        source=source,
        person_id=person_id,
        command=command,
        workflow=workflow,
        attributes={str(k): v for k, v in (attributes or {}).items() if v is not None},
    )
    trace_token = _current_trace.set(ctx)
    span_token = _current_span.set(None)
    try:
        yield ctx
    finally:
        _current_span.reset(span_token)
        _current_trace.reset(trace_token)


@contextlib.contextmanager
def span_scope(name: str) -> Iterator[SpanContext]:
    """Bind a new span (with a fresh ``call_id``) for an individual operation."""
    parent = _current_span.get()
    span = SpanContext(
        span_id=new_id(),
        call_id=new_id(),
        parent_id=parent.span_id if parent is not None else None,
        name=name,
    )
    token = _current_span.set(span)
    try:
        yield span
    finally:
        _current_span.reset(token)


def set_attributes(**values: Any) -> None:
    """Merge attributes into the current trace (``None`` values are ignored)."""
    ctx = _current_trace.get()
    if ctx is None:
        return
    for key, value in values.items():
        if value is not None:
            ctx.attributes[key] = value


def correlation_fields() -> dict[str, Any]:
    """Return the current correlation fields for a diagnostics record."""
    ctx = _current_trace.get()
    span = _current_span.get()
    fields: dict[str, Any] = {
        "trace_id": ctx.trace_id if ctx is not None else None,
        "span_id": span.span_id if span is not None else None,
        "parent_id": span.parent_id if span is not None else None,
        "source": ctx.source if ctx is not None else None,
        "person_id": ctx.person_id if ctx is not None else "",
        "command": ctx.command if ctx is not None else "",
        "workflow": ctx.workflow if ctx is not None else "",
        "attributes": dict(ctx.attributes) if ctx is not None else {},
    }
    if span is not None:
        fields["call_id"] = span.call_id
        fields["span"] = span.name
    return fields


__all__ = [
    "SpanContext",
    "TraceContext",
    "correlation_fields",
    "current_span",
    "current_trace",
    "new_id",
    "set_attributes",
    "span_scope",
    "trace_scope",
]
