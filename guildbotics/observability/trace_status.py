"""Single source of truth for the status of one execution (trace).

The activity timeline and the diagnostics trace list ask the same question —
"what is this execution doing right now?" — so the answer is resolved here
once, next to the records it reads, instead of being folded again by each
screen.

Four layers answer it, strongest first:

1. the dispatch decision (``chat_dispatch.*``): whether anything will retry
2. the rate limit (``workflow.rate_limited``)
3. the recorded workflow completion evidence (``workflow.completed`` /
   ``workflow.completion_missing``). ``retry_scheduled`` needs an actual
   dispatch event, because the ticket workflow shares this completion layer
   but exhausts its attempt budget by posting an error comment instead of
   scheduling a retry, so missing evidence alone resolves to ``incomplete``
4. what the trace's own records show: a failure anywhere, the completion event
   recorded by the layer that opened the trace, or work that has started

Only layer 4's ``TRACE_COMPLETED_EVENT_TYPES`` can make a trace ``success`` on
their own. A finished provider span cannot: it reports that one call returned,
and a chat workflow decides with an LLM call before its agent turn even
starts, so treating that span as the trace's success shows a still-running
execution as finished.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from guildbotics.observability.event_types import TRACE_COMPLETED_EVENT_TYPES

_DISPATCH_DECISIONS = {
    "chat_dispatch.abandoned": "abandoned",
    "chat_dispatch.retry_scheduled": "retry_scheduled",
}
_COMPLETION_EVIDENCE = {
    "workflow.completed": "recorded",
    "workflow.completion_missing": "missing",
}
_ERROR_LOG_LEVELS = frozenset({"ERROR", "CRITICAL"})
_TERMINAL_OBSERVATIONS = frozenset({"failed", "success"})


class TraceStatus:
    """Folds the records of one trace into the status its screens show.

    Records arrive one at a time so a store that accumulates a summary while
    reading its journal and a screen that holds a whole trace in memory share
    the same fold.
    """

    def __init__(self) -> None:
        self._observed = "info"
        self._completion = ""
        self._dispatch = ""
        self._rate_limited = False

    def add(self, item: Mapping[str, Any]) -> None:
        """Fold one diagnostics record into the status."""
        kind = str(item.get("kind") or "")
        if kind == "log":
            if str(item.get("level") or "").upper() in _ERROR_LOG_LEVELS:
                self._observed = "failed"
            return
        if kind != "event":
            return
        event_type = str(item.get("type") or "")
        if event_type == "workflow.rate_limited":
            self._rate_limited = True
        elif event_type in _DISPATCH_DECISIONS:
            self._dispatch = _DISPATCH_DECISIONS[event_type]
        elif event_type in _COMPLETION_EVIDENCE:
            self._completion = _COMPLETION_EVIDENCE[event_type]
        elif event_type.endswith(".failed"):
            self._observed = "failed"
        elif self._observed in _TERMINAL_OBSERVATIONS:
            return
        elif event_type in TRACE_COMPLETED_EVENT_TYPES:
            self._observed = "success"
        elif event_type.endswith((".started", ".finished")):
            # Something ran, but nothing that can speak for the whole trace.
            self._observed = "running"

    def resolve(self) -> str:
        """Return the status for everything folded so far."""
        if self._dispatch == "abandoned":
            return "abandoned"
        if self._rate_limited:
            return "rate_limited"
        if self._dispatch == "retry_scheduled":
            return "retry_scheduled"
        if self._completion == "missing":
            return "incomplete"
        if self._completion == "recorded":
            return "success"
        return self._observed


def resolve_trace_status(records: Iterable[Mapping[str, Any]]) -> str:
    """Return the status of the trace these records belong to."""
    status = TraceStatus()
    for item in records:
        status.add(item)
    return status.resolve()
