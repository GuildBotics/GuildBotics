"""Integration tests for the ``/diagnostics/traces`` endpoints."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.capabilities.member_memory import MemberMemoryService
from guildbotics.capabilities.member_memory_audit import MemoryAuditStore
from guildbotics.entities.team import Person, Project, Team
from guildbotics.observability import span_scope, trace_scope
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR

HEADERS = {"X-GuildBotics-Session-Token": "secret"}
HTTP_OK = 200


def _app(tmp_path: Path) -> tuple[TestClient, EventBus]:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    bus = EventBus(store=store)
    app = create_app(session_token="secret", event_bus=bus, diagnostics_store=store)
    return TestClient(app), bus


def _without_timestamp(item: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in item.items() if key != "timestamp"}


def test_traces_endpoint_lists_published_traces(tmp_path: Path) -> None:
    client, bus = _app(tmp_path)
    with trace_scope("manual", trace_id="t1", command="demo", person_id="alice"):
        bus.publish_event("command.started", {"command": "demo"})
        bus.publish_event("command.finished", {"command": "demo"})

    with client:
        response = client.get("/diagnostics/traces", headers=HEADERS)

    assert response.status_code == HTTP_OK
    traces = response.json()["traces"]
    trace = next(item for item in traces if item["trace_id"] == "t1")
    assert trace["source"] == "manual"
    assert trace["command"] == "demo"
    assert trace["status"] == "success"


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

    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["trace_id"] == "t1"
    assert body["summary"]["status"] == "success"
    kinds = [record["kind"] for record in body["records"]]
    assert kinds == ["event", "log", "event"]


def test_trace_detail_collapses_assistant_streams(tmp_path: Path) -> None:
    client, bus = _app(tmp_path)
    with trace_scope("manual", trace_id="t1"), span_scope("cli_agent"):
        bus.publish_event("agent_runtime.assistant", {"name": "started"})
        bus.publish_event("agent_runtime.assistant", {"name": "delta", "message": "Hel"})
        bus.publish_event("agent_runtime.assistant", {"name": "delta", "message": "lo"})
        bus.publish_event(
            "agent_runtime.assistant", {"name": "completed", "message": "Hello"}
        )

    with client:
        response = client.get("/diagnostics/traces/t1", headers=HEADERS)

    records = response.json()["records"]
    assert [record["payload"]["name"] for record in records] == ["completed"]
    assert records[0]["payload"]["message"] == "Hello"
    assert records[0]["span"] == "cli_agent"


def test_global_endpoint_returns_unscoped_events_and_logs(tmp_path: Path) -> None:
    client, bus = _app(tmp_path)
    with client:
        bus.publish_log("INFO", "global line")
        # A service lifecycle event has no trace and only appears in the global view.
        bus.publish_event("scheduler.running", {"state": "running"}, source="scheduler")
        with trace_scope("manual", trace_id="t1"):
            bus.publish_log("INFO", "scoped line")
        response = client.get("/diagnostics/global", headers=HEADERS)

    records = response.json()["records"]
    messages = [record["message"] for record in records]
    types = [record["type"] for record in records]
    assert "global line" in messages
    assert "scheduler.running" in types
    assert "scoped line" not in messages


def test_activity_history_returns_sessions_and_recorded_github_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    bus = EventBus(store=store)
    runtime = AppRuntime(bus, diagnostics_store=store)
    team = Team(
        project=Project(name="demo"),
        members=[
            Person(
                person_id="alice", name="Alice", person_type="agent", is_active=False
            ),
            Person(person_id="bob", name="Bob", person_type="human", is_active=True),
        ],
    )
    monkeypatch.setattr(runtime, "_get_context", lambda: SimpleNamespace(team=team))
    app = create_app(
        session_token="secret",
        runtime=runtime,
        event_bus=bus,
        diagnostics_store=store,
    )
    with trace_scope(
        "manual",
        trace_id="agent-trace",
        person_id="alice",
        command="demo",
        attributes={
            "github.kind": "issue",
            "github.number": "42",
            "github.url": "https://github.com/owner/repo/issues/42",
        },
    ):
        bus.publish_event("command.started", {"command": "demo"})
        bus.publish_event("command.finished", {"command": "demo"})
    with trace_scope(
        "interactive",
        trace_id="skill-trace",
        person_id="alice",
        command="member memory recall",
    ):
        bus.publish_event("member.command.started", {"command": "member memory recall"})
        bus.publish_event(
            "member.command.finished", {"command": "member memory recall"}
        )
        bus.publish_event(
            "github.pull_request",
            {
                "action": "opened",
                "pull_request": {
                    "number": 8,
                    "title": "Add member activity",
                    "html_url": "https://github.com/owner/repo/pull/8",
                    "merged": False,
                },
            },
        )
        bus.publish_event(
            "github.push",
            {
                "action": "push",
                "ref": "refs/heads/feature",
                "commits": [
                    {
                        "id": "abc1234",
                        "message": "Improve activity history event context\n\nbody",
                        "url": "https://github.com/owner/repo/commit/abc1234",
                    }
                ],
            },
        )
    with trace_scope(
        "manual", trace_id="human-trace", person_id="bob", command="human"
    ):
        bus.publish_event("command.started", {"command": "human"})
    with trace_scope(
        "routine",
        trace_id="empty-routine",
        person_id="alice",
        command="workflows/ticket_driven_workflow",
    ):
        bus.publish_event(
            "command.started", {"command": "workflows/ticket_driven_workflow"}
        )
        bus.publish_event(
            "command.finished", {"command": "workflows/ticket_driven_workflow"}
        )
    bus.publish_event(
        "github.pull_request",
        {
            "action": "closed",
            "pull_request": {
                "number": 7,
                "title": "Add activity",
                "html_url": "https://github.com/owner/repo/pull/7",
                "merged": True,
            },
        },
        source="github",
    )
    bus.publish_event(
        "github.push",
        {
            "ref": "refs/heads/main",
            "compare": "https://github.com/owner/repo/compare/a...b",
            "commits": [
                {
                    "id": "abc",
                    "message": "Add activity page",
                    "url": "https://github.com/owner/repo/commit/abc",
                },
                {
                    "id": "def",
                    "message": "Wire activity API",
                    "url": "https://github.com/owner/repo/commit/def",
                },
            ],
        },
        source="github",
    )
    bus.publish_event(
        "github.issue",
        {
            "action": "closed",
            "issue": {
                "number": 133,
                "title": "Reconnect websocket",
                "html_url": "https://github.com/owner/repo/issues/133",
            },
        },
        source="github",
    )
    client = TestClient(app)

    with client:
        response = client.get(
            "/activity/history",
            params={
                "start": "2000-01-01T00:00:00Z",
                "end": "2999-01-01T00:00:00Z",
            },
            headers=HEADERS,
        )

    assert response.status_code == HTTP_OK
    body = response.json()
    assert [member["person_id"] for member in body["members"]] == ["alice"]
    sessions = {session["trace_id"]: session for session in body["sessions"]}
    assert set(sessions) == {"agent-trace", "skill-trace"}
    assert sessions["agent-trace"]["mode"] == "workflow"
    assert sessions["agent-trace"]["title"] == "Issue #42"
    assert sessions["skill-trace"]["mode"] == "interactive"
    assert sessions["skill-trace"]["title"] == "member memory recall"
    assert [_without_timestamp(link) for link in sessions["agent-trace"]["links"]] == [
        {
            "kind": "issue",
            "label": "Issue #42",
            "url": "https://github.com/owner/repo/issues/42",
        }
    ]
    assert {event["type"] for event in body["events"]} == {
        "issue_resolve",
        "pr_create",
        "pr_merge",
        "push",
    }
    assert any(
        event["title"] == "PR #8 Created" and event["person_id"] == "alice"
        for event in body["events"]
    )
    assert any(
        event["title"] == "Improve activity history event context"
        and event["person_id"] == "alice"
        for event in body["events"]
    )
    member_push = next(
        event
        for event in body["events"]
        if event["title"] == "Improve activity history event context"
    )
    assert [_without_timestamp(link) for link in member_push["links"]] == [
        {
            "kind": "commit",
            "label": "Improve activity history event context",
            "url": "https://github.com/owner/repo/commit/abc1234",
        }
    ]
    assert any(event["title"] == "PR #7 Merged" for event in body["events"])
    shared_push = next(
        event for event in body["events"] if event["title"] == "Push: 2 commits"
    )
    assert [_without_timestamp(link) for link in shared_push["links"]] == [
        {
            "kind": "commit",
            "label": "Add activity page",
            "url": "https://github.com/owner/repo/commit/abc",
        },
        {
            "kind": "commit",
            "label": "Wire activity API",
            "url": "https://github.com/owner/repo/commit/def",
        },
    ]
    assert any(event["title"] == "Issue #133 Resolved" for event in body["events"])
    assert body["unsupported_event_sources"] == []


def test_activity_history_sorts_mixed_timestamp_offsets_by_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    bus = EventBus(store=store)
    runtime = AppRuntime(bus, diagnostics_store=store)
    team = Team(
        project=Project(name="demo"),
        members=[
            Person(person_id="alice", name="Alice", person_type="agent", is_active=True)
        ],
    )
    monkeypatch.setattr(runtime, "_get_context", lambda: SimpleNamespace(team=team))
    store.record(
        {
            "kind": "event",
            "type": "command.finished",
            "timestamp": "2026-07-01T00:30:00Z",
            "trace_id": "mixed-trace",
            "source": "manual",
            "person_id": "alice",
            "command": "demo",
        }
    )
    store.record(
        {
            "kind": "event",
            "type": "command.started",
            "timestamp": "2026-07-01T09:00:00+09:00",
            "trace_id": "mixed-trace",
            "source": "manual",
            "person_id": "alice",
            "command": "demo",
        }
    )

    history = runtime.get_activity_history(
        start="2026-07-01T00:00:00Z",
        end="2026-07-01T01:00:00Z",
    )

    assert len(history.sessions) == 1
    assert history.sessions[0].status == "success"
    assert history.sessions[0].started_at == "2026-07-01T09:00:00+09:00"
    assert history.sessions[0].ended_at == "2026-07-01T00:30:00+00:00"


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
        service.recall(queries=["Retry"])
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

    assert response.status_code == HTTP_OK
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

    with client:
        search_response = client.get(
            "/diagnostics/memory-events",
            params={
                "person_id": "alice",
                "action": "recall",
                "q": "Retry",
            },
            headers=HEADERS,
        )

    assert search_response.status_code == HTTP_OK
    search_payload = search_response.json()
    assert search_payload["event_count"] == 1
    search_event = search_payload["events"][0]
    assert search_event["query_keywords"] == ["Retry"]
    assert search_event["result_count"] == 1
    assert isinstance(search_event["duration_ms"], float)
    assert search_event["body_preview"] == ""

    with client:
        trace_response = client.get(
            "/diagnostics/memory-events",
            params={"trace_id": "memory-trace"},
            headers=HEADERS,
        )

    assert trace_response.status_code == HTTP_OK
    assert {event["trace_id"] for event in trace_response.json()["events"]} == {
        "memory-trace"
    }


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

    assert response.status_code == HTTP_OK
    records = response.json()["records"]
    assert [record["kind"] for record in records] == ["event", "memory", "event"]
    assert records[1]["type"] == "memory.record"
