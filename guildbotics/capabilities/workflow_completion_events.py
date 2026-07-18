"""Diagnostics events that separate workflow completion from provider success.

A provider turn can finish cleanly (``returncode: 0``, ``span.finished``)
while the workflow never records its required completion evidence, and the
dispatcher then retries or abandons the event.  These helpers record each of
those layers as an explicit event so diagnostics can tell them apart:

- ``workflow.completed`` / ``workflow.completion_missing`` — whether the run's
  completion evidence exists in the run store after an agent turn.
- ``chat_dispatch.retry_scheduled`` / ``chat_dispatch.abandoned`` — what the
  pending chat dispatcher decided to do with the event afterwards.

Correlation (trace id, person, service, channel, thread, event id) comes from
the active trace scope; payloads carry the run-level facts.
"""

from __future__ import annotations

from guildbotics.observability.diagnostics_events import record_correlated_event


def record_workflow_completed(
    *,
    run_id: str,
    attempt: int = 0,
    recovered: bool = False,
) -> None:
    """Record that the run's completion evidence exists in the run store.

    ``recovered=True`` marks a re-dispatch that found the evidence already
    recorded (crash between completion and dispatch bookkeeping) and therefore
    skipped the agent.
    """
    record_correlated_event(
        event_type="workflow.completed",
        attributes={"workflow.completion": "recorded"},
        payload={
            "run_id": run_id,
            "attempt": attempt,
            "recovered": recovered,
        },
    )


def record_workflow_completion_missing(
    *,
    run_id: str,
    attempt: int,
    max_attempts: int,
    error: str,
) -> None:
    """Record a provider turn that ended without completion evidence."""
    record_correlated_event(
        event_type="workflow.completion_missing",
        attributes={"workflow.completion": "missing"},
        payload={
            "run_id": run_id,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "error": error,
        },
    )


def record_chat_dispatch_retry_scheduled(
    *,
    event_id: str,
    run_id: str,
    attempt_count: int,
    max_attempts: int,
    next_attempt_at: str,
    error_category: str,
) -> None:
    """Record that the pending queue kept the event for a later retry."""
    record_correlated_event(
        event_type="chat_dispatch.retry_scheduled",
        attributes={"chat_dispatch.state": "retry_scheduled"},
        payload={
            "event_id": event_id,
            "run_id": run_id,
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "next_attempt_at": next_attempt_at,
            "error_category": error_category,
        },
    )


def record_chat_dispatch_abandoned(
    *,
    event_id: str,
    run_id: str,
    attempt_count: int,
    max_attempts: int,
    error: str,
) -> None:
    """Record that the event was terminalized after its final attempt.

    The dispatcher increments its attempt counter before running the workflow,
    so ``attempt_count`` can arrive one higher than ``max_attempts`` for the
    attempt that triggered abandonment. The diagnostics payload caps it at
    ``max_attempts`` so it never reads as "attempt 6 of 5".
    """
    record_correlated_event(
        event_type="chat_dispatch.abandoned",
        attributes={"chat_dispatch.state": "abandoned"},
        payload={
            "event_id": event_id,
            "run_id": run_id,
            "attempt_count": min(attempt_count, max_attempts),
            "max_attempts": max_attempts,
            "error": error,
        },
    )
