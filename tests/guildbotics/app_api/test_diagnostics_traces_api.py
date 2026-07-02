"""Integration tests for the ``/diagnostics/traces`` endpoints."""

from __future__ import annotations

import json
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
from guildbotics.observability import trace_scope
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR

HEADERS = {"X-GuildBotics-Session-Token": "secret"}
HTTP_OK = 200


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

    assert response.status_code == HTTP_OK
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


def test_activity_history_filters_prompt_trace_before_limit(
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
    events = [
        {
            "event": "prompt.completed",
            "timestamp": "2026-06-01T10:00:00Z",
            "trace_id": "old-prompt",
            "source": "prompt_trace",
            "person_id": "alice",
            "command": "old command",
        }
    ]
    events.extend(
        {
            "event": "prompt.completed",
            "timestamp": f"2026-07-01T{index % 24:02d}:00:00Z",
            "trace_id": f"recent-{index}",
            "source": "prompt_trace",
            "person_id": "alice",
            "command": "recent command",
        }
        for index in range(1001)
    )
    monkeypatch.setattr(runtime, "_read_all_prompt_trace_events", lambda: events)

    history = runtime.get_activity_history(
        start="2026-06-01T00:00:00Z",
        end="2026-06-02T00:00:00Z",
        limit=1000,
    )

    assert [session.trace_id for session in history.sessions] == ["old-prompt"]


def test_activity_history_merges_memory_and_prompt_trace_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(tmp_path / "data"))
    prompt_trace = tmp_path / "prompt_trace.jsonl"
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE_PATH", str(prompt_trace))
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

    MemoryAuditStore().record(
        {
            "kind": "memory",
            "type": "memory.write",
            "timestamp": "2026-07-01T10:00:00+00:00",
            "trace_id": "memory-trace",
            "source": "manual",
            "person_id": "alice",
            "command": "functions/talk_as",
            "message": "memory write: Desktop API 仕様書",
            "attributes": {
                "memory.action": "write",
                "memory.doc_id": "desktop-api",
                "memory.path": "documents/desktop-api",
                "memory.scope": "project",
            },
            "payload": {
                "title": "Desktop API 仕様書",
                "summary": "API notes",
                "source": [
                    {
                        "type": "pr",
                        "url": "https://github.com/owner/repo/pull/240",
                    }
                ],
            },
        }
    )
    prompt_trace.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "cli_agent.request",
                        "timestamp": "2026-07-01T11:00:00+00:00",
                        "trace_id": "prompt-trace",
                        "source": "manual",
                        "person_id": "alice",
                        "command": "functions/talk_as",
                        "brain": "Codex",
                        "prompt": "調査して",
                    }
                ),
                json.dumps(
                    {
                        "event": "cli_agent.response",
                        "timestamp": "2026-07-01T11:05:00+00:00",
                        "trace_id": "prompt-trace",
                        "source": "manual",
                        "person_id": "alice",
                        "command": "functions/talk_as",
                        "brain": "Codex",
                        "stdout": "done",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    history = runtime.get_activity_history(
        start="2026-07-01T00:00:00Z",
        end="2026-07-02T00:00:00Z",
    )

    sessions = {session.trace_id: session for session in history.sessions}
    assert set(sessions) == {"memory-trace", "prompt-trace"}
    assert [link.model_dump() for link in sessions["memory-trace"].links] == [
        {
            "kind": "pull_request",
            "label": "Desktop API 仕様書",
            "url": "https://github.com/owner/repo/pull/240",
            "timestamp": "2026-07-01T10:00:00+00:00",
        },
        {
            "kind": "doc",
            "label": "Desktop API 仕様書",
            "url": (
                "/diagnostics?tab=memory&doc_id=desktop-api&trace_id=memory-trace"
                "&timestamp=2026-07-01T10%3A00%3A00%2B00%3A00"
                "&action=write&person_id=alice"
            ),
            "timestamp": "2026-07-01T10:00:00+00:00",
        },
    ]
    assert sessions["prompt-trace"].mode == "workflow"
    assert sessions["memory-trace"].title == "Desktop API 仕様書"
    assert sessions["prompt-trace"].title == "調査して"


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
