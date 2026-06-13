"""Unit tests for the correlation core (``guildbotics.observability``)."""

from __future__ import annotations

from guildbotics.observability import (
    correlation_fields,
    current_trace,
    set_attributes,
    span_scope,
    trace_scope,
)


def test_no_trace_yields_empty_correlation() -> None:
    fields = correlation_fields()
    assert fields["trace_id"] is None
    assert fields["source"] is None
    assert fields["attributes"] == {}
    assert "call_id" not in fields


def test_trace_scope_sets_correlation_fields() -> None:
    with trace_scope(
        "routine",
        person_id="alice",
        command="workflows/demo",
        attributes={"service_run_id": "svc-1"},
        trace_id="trace-1",
    ):
        fields = correlation_fields()
        assert fields["trace_id"] == "trace-1"
        assert fields["source"] == "routine"
        assert fields["person_id"] == "alice"
        assert fields["command"] == "workflows/demo"
        assert fields["attributes"] == {"service_run_id": "svc-1"}
    # Restored to no-trace after the scope exits.
    assert current_trace() is None


def test_trace_scope_drops_none_attributes() -> None:
    with trace_scope(
        "event_listener", attributes={"service_run_id": None, "slack.ts": "12.3"}
    ):
        assert correlation_fields()["attributes"] == {"slack.ts": "12.3"}


def test_span_scope_adds_span_and_call_id() -> None:
    with trace_scope("manual", trace_id="trace-1"):
        with span_scope("llm"):
            fields = correlation_fields()
            assert fields["trace_id"] == "trace-1"
            assert fields["span_id"]
            assert fields["parent_id"] is None
            assert fields["call_id"]
            first_call_id = fields["call_id"]
        # Span is unwound after the block.
        assert correlation_fields()["span_id"] is None
        with span_scope("llm"):
            assert correlation_fields()["call_id"] != first_call_id


def test_span_name_is_exposed_in_correlation_fields() -> None:
    with trace_scope("manual", trace_id="trace-1"):
        assert "span" not in correlation_fields()
        with span_scope("cli_agent"):
            assert correlation_fields()["span"] == "cli_agent"
        assert "span" not in correlation_fields()


def test_nested_span_sets_parent_id() -> None:
    with trace_scope("manual", trace_id="trace-1"), span_scope("outer") as outer:
        with span_scope("inner"):
            assert correlation_fields()["parent_id"] == outer.span_id


def test_trace_scope_resets_inherited_span() -> None:
    with trace_scope("manual"), span_scope("outer"):
        with trace_scope("event_listener", trace_id="trace-2"):
            # A new trace must not inherit the outer span.
            assert correlation_fields()["span_id"] is None


def test_set_attributes_merges_into_current_trace() -> None:
    with trace_scope("event_listener", trace_id="trace-1"):
        set_attributes(**{"github.issue": "42", "ignored": None})
        attributes = correlation_fields()["attributes"]
        assert attributes["github.issue"] == "42"
        assert "ignored" not in attributes


def test_set_attributes_without_trace_is_noop() -> None:
    set_attributes(**{"github.issue": "42"})
    assert correlation_fields()["attributes"] == {}
