"""Unit tests for the correlated event recorders."""

from __future__ import annotations

from typing import Any

from guildbotics.observability import diagnostics_events


def _recorded(monkeypatch, **kwargs: Any) -> dict[str, Any]:
    """Record one span summary against a stubbed store and return the record."""
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(
        diagnostics_events,
        "_store",
        lambda: type("S", (), {"record": staticmethod(records.append)})(),
    )
    diagnostics_events.record_span_summary(**kwargs)
    return records[0]


def test_span_summary_payload_states_model_effort_and_duration(monkeypatch) -> None:
    record = _recorded(
        monkeypatch,
        model="claude-sonnet-5",
        effort="high",
        duration_ms=12_345.6789,
        usage={"input_tokens": 10},
        attributes={"agent.kind": "cli_agent", "agent.slot": "default"},
    )

    assert record["type"] == "span.finished"
    assert record["payload"] == {
        "model": "claude-sonnet-5",
        "effort": "high",
        "usage": {"input_tokens": 10},
        "duration_ms": 12_345.679,
    }
    assert record["attributes"] == {"agent.kind": "cli_agent", "agent.slot": "default"}


def test_span_summary_keeps_the_effort_key_even_when_nothing_was_applied(
    monkeypatch,
) -> None:
    """Consumers read a fixed shape, so an unknown value is empty, not absent."""
    record = _recorded(monkeypatch, status="failed", model="default")

    assert record["type"] == "span.failed"
    assert record["payload"] == {"model": "default", "effort": "", "usage": {}}
