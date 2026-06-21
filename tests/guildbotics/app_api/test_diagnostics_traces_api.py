"""Integration tests for the ``/diagnostics/traces`` endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.diagnostics_store import DiagnosticsStore
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.capabilities.member_memory import MemberMemoryService
from guildbotics.entities.team import Person
from guildbotics.observability import trace_scope
from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR

HEADERS = {"X-GuildBotics-Session-Token": "secret"}


@pytest.fixture(autouse=True)
def _isolate_prompt_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point the prompt-trace path at an absent file so list_traces does not read
    # a real prompt_trace.jsonl from the developer's storage.
    monkeypatch.delenv("GUILDBOTICS_PROMPT_TRACE", raising=False)
    monkeypatch.setenv(
        "GUILDBOTICS_PROMPT_TRACE_PATH", str(tmp_path / "absent_prompt_trace.jsonl")
    )


def _app(tmp_path: Path) -> tuple[TestClient, EventBus]:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    bus = EventBus(store=store)
    app = create_app(session_token="secret", event_bus=bus, diagnostics_store=store)
    return TestClient(app), bus


def test_traces_endpoint_lists_published_traces(tmp_path: Path) -> None:
    client, bus = _app(tmp_path)
    with trace_scope("manual", trace_id="t1", command="demo", person_id="alice"):
        bus.publish_event("command.started", {"command": "demo"})
        bus.publish_event("command.finished", {"command": "demo"})

    with client:
        response = client.get("/diagnostics/traces", headers=HEADERS)

    assert response.status_code == 200
    traces = response.json()["traces"]
    assert len(traces) == 1
    assert traces[0]["trace_id"] == "t1"
    assert traces[0]["source"] == "manual"
    assert traces[0]["command"] == "demo"
    assert traces[0]["status"] == "success"


def test_traces_endpoint_filters_by_source(tmp_path: Path) -> None:
    client, bus = _app(tmp_path)
    with trace_scope("manual", trace_id="t1"):
        bus.publish_event("command.started", {})
    with trace_scope("routine", trace_id="t2"):
        bus.publish_event("command.started", {})

    with client:
        response = client.get(
            "/diagnostics/traces", params={"source": "routine"}, headers=HEADERS
        )

    traces = response.json()["traces"]
    assert {trace["trace_id"] for trace in traces} == {"t2"}


def test_traces_endpoint_filters_by_exact_attribute(tmp_path: Path) -> None:
    client, bus = _app(tmp_path)
    with trace_scope("routine", trace_id="t1", attributes={"github.number": "42"}):
        bus.publish_event("command.started", {})
    with trace_scope("routine", trace_id="t2", attributes={"github.number": "7"}):
        bus.publish_event("command.started", {})

    with client:
        response = client.get(
            "/diagnostics/traces",
            params={"attr_key": "github.number", "attr_value": "42"},
            headers=HEADERS,
        )

    assert {t["trace_id"] for t in response.json()["traces"]} == {"t1"}


def test_trace_detail_returns_ordered_records(tmp_path: Path) -> None:
    client, bus = _app(tmp_path)
    with trace_scope("manual", trace_id="t1"):
        bus.publish_event("command.started", {"command": "demo"})
        bus.publish_log("INFO", "working")
        bus.publish_event("command.finished", {"command": "demo"})

    with client:
        response = client.get("/diagnostics/traces/t1", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"] == "t1"
    assert body["summary"]["status"] == "success"
    kinds = [record["kind"] for record in body["records"]]
    assert kinds == ["event", "log", "event"]


def test_prompt_only_traces_only_appear_under_all_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    bus = EventBus(store=store)
    runtime = AppRuntime(bus, diagnostics_store=store)
    with trace_scope("manual", trace_id="m1"):
        bus.publish_event("command.started", {})
    # "p1" exists only as a prompt-trace record (no store events/logs).
    monkeypatch.setattr(runtime, "_prompt_trace_trace_ids", lambda: {"p1", "m1"})

    # Unfiltered ("all") surfaces the prompt-only trace alongside the real one.
    all_ids = {trace.trace_id for trace in runtime.list_traces().traces}
    assert {"m1", "p1"} <= all_ids

    # A source filter must not leak the prompt-only trace (unknown source).
    routine_ids = {
        trace.trace_id for trace in runtime.list_traces(source="routine").traces
    }
    assert "p1" not in routine_ids
    manual_ids = {
        trace.trace_id for trace in runtime.list_traces(source="manual").traces
    }
    assert "p1" not in manual_ids
    assert "m1" in manual_ids


def test_global_endpoint_returns_unscoped_events_and_logs(tmp_path: Path) -> None:
    client, bus = _app(tmp_path)
    bus.publish_log("INFO", "global line")
    # A service lifecycle event has no trace and only appears in the global view.
    bus.publish_event("scheduler.running", {"state": "running"}, source="scheduler")
    with trace_scope("manual", trace_id="t1"):
        bus.publish_log("INFO", "scoped line")

    with client:
        response = client.get("/diagnostics/global", headers=HEADERS)

    records = response.json()["records"]
    messages = [record["message"] for record in records]
    types = [record["type"] for record in records]
    assert "global line" in messages
    assert "scheduler.running" in types
    assert "scoped line" not in messages


def test_memory_events_endpoint_filters_and_returns_body_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(tmp_path / "data"))
    person = Person(person_id="alice", name="Alice", person_type="human")
    service = MemberMemoryService(person)
    with trace_scope("manual", trace_id="memory-trace", person_id="alice"):
        recorded = service.record(
            scope="personal",
            title="Retry note",
            summary="Refresh before retry.",
            source=[{"type": "ticket", "url": "https://example.test/issues/42"}],
            body="Retry after refreshing the token.",
        )
        service.touch(doc_id=recorded["doc_id"])
    client, _bus = _app(tmp_path)

    with client:
        response = client.get(
            "/diagnostics/memory-events",
            params={
                "person_id": "alice",
                "action": "record",
                "source": "issues/42",
                "q": "Retry",
            },
            headers=HEADERS,
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["event_count"] == 1
    event = payload["events"][0]
    assert event["action"] == "record"
    assert event["person_id"] == "alice"
    assert event["doc_id"] == recorded["doc_id"]
    assert event["trace_id"] == "memory-trace"
    assert event["source"] == [
        {"type": "ticket", "url": "https://example.test/issues/42"}
    ]
    assert event["body_preview"] == "Retry after refreshing the token."


def test_trace_detail_merges_memory_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(tmp_path / "data"))
    person = Person(person_id="alice", name="Alice", person_type="human")
    service = MemberMemoryService(person)
    client, bus = _app(tmp_path)
    with trace_scope("manual", trace_id="memory-trace", person_id="alice"):
        bus.publish_event("command.started", {"command": "demo"})
        service.record(scope="personal", title="Trace memory", body="body")
        bus.publish_event("command.finished", {"command": "demo"})

    with client:
        response = client.get("/diagnostics/traces/memory-trace", headers=HEADERS)

    assert response.status_code == 200
    records = response.json()["records"]
    assert [record["kind"] for record in records] == ["event", "memory", "event"]
    assert records[1]["type"] == "memory.record"
