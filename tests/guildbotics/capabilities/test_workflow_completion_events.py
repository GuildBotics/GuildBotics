from __future__ import annotations

from typing import Any

from guildbotics.capabilities import workflow_completion_events


def _capture(monkeypatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        workflow_completion_events,
        "record_correlated_event",
        lambda **kwargs: recorded.append(kwargs),
    )
    return recorded


def test_abandoned_caps_attempt_count_at_max_attempts(monkeypatch) -> None:
    # The dispatcher increments its attempt counter before running the
    # workflow, so the caller can legitimately pass attempt_count one higher
    # than max_attempts (e.g. 6 of 5) for the attempt that triggers
    # abandonment. The diagnostics payload must not surface that raw overrun.
    recorded = _capture(monkeypatch)

    workflow_completion_events.record_chat_dispatch_abandoned(
        event_id="E1",
        run_id="run-1",
        attempt_count=6,
        max_attempts=5,
        error="boom",
    )

    assert len(recorded) == 1
    payload = recorded[0]["payload"]
    assert payload["attempt_count"] == 5
    assert payload["max_attempts"] == 5


def test_abandoned_keeps_attempt_count_within_budget(monkeypatch) -> None:
    recorded = _capture(monkeypatch)

    workflow_completion_events.record_chat_dispatch_abandoned(
        event_id="E1",
        run_id="run-1",
        attempt_count=3,
        max_attempts=5,
        error="boom",
    )

    assert recorded[0]["payload"]["attempt_count"] == 3
