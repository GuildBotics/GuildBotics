"""The trace and device-record readings (``traces.py``) and their endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api import runtime as runtime_module
from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.models import (
    ConfigStatus,
    ScenarioDiagnosticsResponse,
    VerifyResponse,
)
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.app_api.traces import last_cli_agent_turns, latest_inference_failures
from guildbotics.capabilities.member_memory import MemberMemoryService
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.entities.team import Person, Project, Team
from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.observability.interactive_sessions import (
    InteractiveSessionStore,
    InteractiveTraceSession,
)
from guildbotics.utils.correlation import current_trace, span_scope, trace_scope

HEADERS = {"X-GuildBotics-Session-Token": "secret"}
HTTP_OK = 200


def _app(tmp_path: Path) -> tuple[TestClient, EventBus]:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    bus = EventBus(store=store)
    app = create_app(session_token="secret", runtime=AppRuntime(bus))
    return TestClient(app), bus


def _runtime_app(
    tmp_path: Path,
) -> tuple[TestClient, EventBus, AppRuntime, DiagnosticsStore]:
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    bus = EventBus(store=store)
    runtime = AppRuntime(bus)
    app = create_app(
        session_token="secret",
        runtime=runtime,
    )
    return TestClient(app), bus, runtime, store


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
    assert trace["title"] == "demo"
    assert trace["status"] == "success"


def test_traces_endpoint_titles_a_trace_by_its_work_target(tmp_path: Path) -> None:
    # The execution list and the activity timeline answer "what is this
    # execution working on?" the same way: the first PR / issue recorded in
    # the trace, by title.
    client, bus = _app(tmp_path)
    with trace_scope(
        "routine",
        trace_id="t-ticket",
        command="workflows/ticket_driven_workflow",
        person_id="alice",
        attributes={
            "target.kind": "ticket",
            "target.label": "#544",
            "target.url": "https://github.com/owner/repo/issues/544",
            "target.title": "診断ログの実行タイトルに作業対象を表示する",
        },
    ):
        bus.publish_event("command.started", {"command": "demo"})

    with client:
        traces = client.get("/diagnostics/traces", headers=HEADERS).json()["traces"]
        detail = client.get("/diagnostics/traces/t-ticket", headers=HEADERS).json()

    listed = next(item for item in traces if item["trace_id"] == "t-ticket")
    assert listed["title"] == "診断ログの実行タイトルに作業対象を表示する"
    assert detail["summary"]["title"] == listed["title"]


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


def test_verify_is_recorded_as_a_diagnostics_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, bus, runtime, store = _runtime_app(tmp_path)
    config = ConfigStatus(
        cwd=tmp_path,
        workspace=tmp_path,
        config_dir=tmp_path / "config",
        project_file=tmp_path / "config/team/project.yml",
        project_file_exists=False,
        storage_dir=tmp_path,
    )
    monkeypatch.setattr(runtime, "get_config_status", lambda: config)
    monkeypatch.setattr(
        runtime,
        "_get_context",
        lambda: SimpleNamespace(team=Team(project=Project(name="demo"), members=[])),
    )

    def _verify(*_args: object, **_kwargs: object) -> VerifyResponse:
        bus.publish_log("INFO", "verification log")
        return VerifyResponse(
            ok=True,
            config=config,
            active_members=[],
            checks=[],
            warnings=[],
            errors=[],
        )

    monkeypatch.setattr(runtime_module.VerifyService, "verify", _verify)

    with client:
        assert client.post("/verify", headers=HEADERS).status_code == HTTP_OK
        response = client.get(
            "/diagnostics/traces",
            params={"source": "diagnostics"},
            headers=HEADERS,
        )

    traces = response.json()["traces"]
    assert len(traces) == 1
    assert traces[0]["source"] == "diagnostics"
    assert traces[0]["command"] == "verify"
    records = store.get_records(traces[0]["trace_id"])
    assert [record.get("message") for record in records if record["kind"] == "log"] == [
        "verification log"
    ]
    assert [record.get("type") for record in records if record["kind"] == "event"] == [
        "verify.completed"
    ]


def test_scenario_diagnostics_is_recorded_as_a_diagnostics_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, bus, runtime, store = _runtime_app(tmp_path)

    class _Context:
        team = Team(
            project=Project(services={"chat_service": {"name": "slack"}}), members=[]
        )

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(runtime, "_get_context", _Context)

    async def _run(*_args: object, **_kwargs: object) -> ScenarioDiagnosticsResponse:
        bus.publish_log("WARNING", "scenario diagnostics log")
        return ScenarioDiagnosticsResponse(
            ok=True,
            active_members=["alice"],
            checks=[],
            warnings=[],
            errors=[],
        )

    monkeypatch.setattr(runtime_module.ScenarioDiagnosticsService, "run", _run)

    with client:
        assert (
            client.post(
                "/diagnostics/scenario",
                params={"person_id": "alice"},
                headers=HEADERS,
            ).status_code
            == HTTP_OK
        )
        response = client.get(
            "/diagnostics/traces",
            params={"source": "diagnostics"},
            headers=HEADERS,
        )

    traces = response.json()["traces"]
    assert len(traces) == 1
    assert traces[0]["source"] == "diagnostics"
    assert traces[0]["person_id"] == "alice"
    assert traces[0]["command"] == "diagnostics"
    records = store.get_records(traces[0]["trace_id"])
    assert [record.get("message") for record in records if record["kind"] == "log"] == [
        "scenario diagnostics log"
    ]
    events = [record for record in records if record["kind"] == "event"]
    assert [record.get("type") for record in events] == ["diagnostics.completed"]
    # The configured providers travel with the outcome, so that the alerts of
    # a provider no longer used can close.
    assert events[0]["payload"]["providers"] == ["slack"]


def test_traces_endpoint_filters_by_exact_attribute(tmp_path: Path) -> None:
    client, bus = _app(tmp_path)
    with trace_scope("routine", trace_id="t1", attributes={"target.label": "#42"}):
        bus.publish_event("command.started", {})
    with trace_scope("routine", trace_id="t2", attributes={"target.label": "#7"}):
        bus.publish_event("command.started", {})

    with client:
        response = client.get(
            "/diagnostics/traces",
            params={"attr_key": "target.label", "attr_value": "#42"},
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
    assert body["records"][0]["presentation"] == {
        "label_key": "diagnostics.executions.eventTypes.command_started",
        "label_fallback": "command.started",
        "message_key": "",
        "message": "demo",
        "message_params": {},
        "tone": "success",
        "effort": "",
    }
    assert body["records"][1]["presentation"]["message"] == "working"


def test_trace_detail_collapses_assistant_streams(tmp_path: Path) -> None:
    client, bus = _app(tmp_path)
    with trace_scope("manual", trace_id="t1"), span_scope("cli_agent"):
        bus.publish_event("agent_runtime.assistant", {"name": "started"})
        bus.publish_event(
            "agent_runtime.assistant", {"name": "delta", "message": "Hel"}
        )
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
    runtime = AppRuntime(bus)
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
    # These tests read recorded history; GitHub refresh has its own tests, and
    # its thread would outlive the test.
    monkeypatch.setattr(runtime, "_refresh_activity_events", lambda *_a, **_k: None)
    activity = ActivityEventStore()

    def record_github(
        event_type: str,
        payload: dict,
        *,
        person_id: str = "",
        source: str = "",
    ) -> None:
        activity.record(
            {
                "type": event_type,
                "payload": payload,
                "person_id": person_id,
                "source": source,
            }
        )

    app = create_app(
        session_token="secret",
        runtime=runtime,
    )
    # A workflow run is its shared task-run record, mirrored from its trace by
    # the execution boundary; its command events stay in local diagnostics.
    with trace_scope(
        "routine",
        trace_id="agent-trace",
        person_id="alice",
        command="demo",
        attributes={
            "target.kind": "ticket",
            "target.label": "#42",
            "target.url": "https://github.com/owner/repo/issues/42",
        },
    ):
        bus.publish_event("command.started", {"command": "demo"})
        RunStore().start_record(
            "agent-trace",
            work_kind="demo",
            execution_mode="autonomous",
            member_id="alice",
            source="routine",
            attributes=current_trace().attributes if current_trace() else {},
        )
        bus.publish_event("command.finished", {"command": "demo"})
        RunStore().finish_record("agent-trace", status="succeeded")
    # An interactive session is its shared session record.
    skill_session = InteractiveTraceSession(
        trace_id="skill-trace",
        person_id="alice",
        workspace=str(tmp_path),
        host="codex",
        thread_key="thread-1",
        started_at=datetime.now(UTC).isoformat(),
        last_seen_at=datetime.now(UTC).isoformat(),
        expires_at=datetime.now(UTC).isoformat(),
    )
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
        InteractiveSessionStore().record(
            skill_session, command="member memory recall", status="success"
        )
        pr_opened = {
            "action": "opened",
            "pull_request": {
                "label": "#8",
                "title": "Add member activity",
                "url": "https://github.com/owner/repo/pull/8",
                "merged": False,
            },
        }
        bus.publish_event("pull_request.item", pr_opened)
        record_github("pull_request.item", pr_opened, person_id="alice")
        push_payload = {
            "action": "push",
            "ref": "refs/heads/feature",
            "commits": [
                {
                    "id": "abc1234",
                    "message": "Improve activity history event context\n\nbody",
                    "url": "https://github.com/owner/repo/commit/abc1234",
                }
            ],
        }
        bus.publish_event("repository.push", push_payload)
        record_github("repository.push", push_payload, person_id="alice")
    InteractiveSessionStore().record(
        InteractiveTraceSession(
            trace_id="human-trace",
            person_id="bob",
            workspace=str(tmp_path),
            host="codex",
            thread_key="thread-2",
            started_at=datetime.now(UTC).isoformat(),
            last_seen_at=datetime.now(UTC).isoformat(),
            expires_at=datetime.now(UTC).isoformat(),
        ),
        command="human",
        status="success",
    )
    # A scheduled command that ran and touched nothing is not activity.
    RunStore().start_record(
        "empty-routine",
        work_kind="daily-report",
        execution_mode="autonomous",
        member_id="alice",
        source="scheduled",
    )
    RunStore().finish_record("empty-routine", status="succeeded")
    merged = {
        "action": "closed",
        "pull_request": {
            "label": "#7",
            "title": "Add activity",
            "url": "https://github.com/owner/repo/pull/7",
            "merged": True,
        },
    }
    bus.publish_event("pull_request.item", merged, source="github")
    record_github("pull_request.item", merged, source="github")
    main_push = {
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
    }
    bus.publish_event("repository.push", main_push, source="github")
    record_github("repository.push", main_push, source="github")
    issue_closed = {
        "action": "closed",
        "ticket": {
            "label": "#133",
            "title": "Reconnect websocket",
            "url": "https://github.com/owner/repo/issues/133",
        },
    }
    bus.publish_event("ticket.item", issue_closed, source="github")
    record_github("ticket.item", issue_closed, source="github")
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
    assert sessions["agent-trace"]["title"] == "Ticket #42"
    assert sessions["agent-trace"]["status"] == "success"
    assert sessions["skill-trace"]["mode"] == "interactive"
    assert sessions["skill-trace"]["title"] == "member memory recall"
    assert sessions["skill-trace"]["status"] == "success"
    assert [_without_timestamp(link) for link in sessions["agent-trace"]["links"]] == [
        {
            "kind": "ticket",
            "label": "Ticket #42",
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
    assert any(event["title"] == "Ticket #133 Resolved" for event in body["events"])
    assert body["unsupported_event_sources"] == []


def test_activity_history_reads_sessions_from_their_lifecycle_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sessions come from the shared lifecycle records, in the window asked."""
    store = DiagnosticsStore(tmp_path / "diag.jsonl")
    bus = EventBus(store=store)
    runtime = AppRuntime(bus)
    team = Team(
        project=Project(name="demo"),
        members=[
            Person(person_id="alice", name="Alice", person_type="agent", is_active=True)
        ],
    )
    monkeypatch.setattr(runtime, "_get_context", lambda: SimpleNamespace(team=team))
    # These tests read recorded history; GitHub refresh has its own tests, and
    # its thread would outlive the test.
    monkeypatch.setattr(runtime, "_refresh_activity_events", lambda *_a, **_k: None)
    InteractiveSessionStore().record(
        InteractiveTraceSession(
            trace_id="mixed-trace",
            person_id="alice",
            workspace=str(tmp_path),
            host="codex",
            thread_key="thread-1",
            started_at="2026-07-01T09:00:00+09:00",
            last_seen_at="2026-07-01T09:00:00+09:00",
            expires_at="2026-07-01T09:30:00+09:00",
        ),
        command="demo",
        status="failed",
        now=datetime(2026, 7, 1, 0, 30, tzinfo=UTC),
    )
    # A command boundary event alone is not a session any more.
    store.record(
        {
            "kind": "event",
            "type": "command.finished",
            "timestamp": "2026-07-01T00:40:00Z",
            "trace_id": "boundary-only",
            "source": "interactive",
            "person_id": "alice",
            "command": "demo",
        }
    )

    history = runtime.get_activity_history(
        start="2026-07-01T00:00:00Z",
        end="2026-07-01T01:00:00Z",
    )

    assert [session.trace_id for session in history.sessions] == ["mixed-trace"]
    assert history.sessions[0].status == "failed"
    assert history.sessions[0].started_at == "2026-07-01T09:00:00+09:00"
    assert history.sessions[0].ended_at == "2026-07-01T00:30:00+00:00"
    assert history.sessions[0].duration_seconds == 1800.0


def test_memory_events_endpoint_filters_and_returns_body_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def _turn(
    store: DiagnosticsStore,
    *,
    person: str = "alice",
    tool: str = "codex",
    model: str = "actual",
    effort: str = "",
    specified: bool | None = False,
    timestamp: str = "2026-09-29T10:00:00Z",
    status: str = "finished",
    slot: str = "default",
) -> None:
    store.record(
        {
            "kind": "event",
            "type": f"span.{status}",
            "person_id": person,
            "timestamp": timestamp,
            "attributes": {
                "agent.kind": "cli_agent",
                "agent.adapter": tool,
                "agent.slot": slot,
            },
            "payload": {"model": model, "model_specified": specified, "effort": effort},
        }
    )


def _call(
    store: DiagnosticsStore,
    status: str,
    timestamp: str,
    *,
    service: str = "llm",
    provider: str = "openai",
    category: str = "",
) -> None:
    store.record(
        {
            "kind": "event",
            "type": f"span.{status}",
            "timestamp": timestamp,
            "attributes": {
                "credential.provider": service,
                **({"llm.provider": provider} if provider else {}),
                **(
                    {
                        "error.category": category,
                        "error.status_code": 429,
                        "error.response": f"{category} answer",
                    }
                    if category
                    else {}
                ),
            },
            "payload": {},
        }
    )


def test_latest_finished_or_failed_turn_per_member_and_tool(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl", memory_limit=1)
    _turn(store, model="old", effort="low")
    _turn(store, tool="claude", model="sonnet", specified=True, effort="high")
    _turn(store, person="bob", model="bob-model", effort="xhigh")
    # Unknown effective values on the latest turn must replace an older model.
    _turn(
        store,
        model="",
        status="failed",
        slot="reviewer",
        timestamp="2026-09-29T12:00:00Z",
    )
    # Arrival order and spelling of the timezone must not decide recency.
    _turn(store, model="late-arriving-old", timestamp="2026-09-29T19:30:00+09:00")
    _turn(
        store, model="still-running", status="started", timestamp="2026-09-30T00:00:00Z"
    )

    turns = {(turn.person_id, turn.agent): turn for turn in last_cli_agent_turns(store)}
    assert set(turns) == {("alice", "codex"), ("alice", "claude"), ("bob", "codex")}
    assert turns["alice", "codex"].model == ""
    assert turns["alice", "codex"].effort == ""
    assert turns["alice", "codex"].timestamp == "2026-09-29T12:00:00Z"
    assert turns["alice", "claude"].model == "sonnet"
    assert turns["alice", "claude"].model_specified is True
    assert turns["alice", "claude"].effort == "high"
    assert turns["bob", "codex"].model == "bob-model"
    assert turns["bob", "codex"].model_specified is False
    assert turns["bob", "codex"].effort == "xhigh"


@pytest.mark.parametrize(
    "overrides",
    [
        {"person": ""},
        {"tool": ""},
        {"specified": None},
        {"timestamp": "invalid"},
    ],
)
def test_incomplete_turn_facts_are_not_inferred_from_current_settings(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    _turn(store, **overrides)
    assert last_cli_agent_turns(store) == []


def test_each_keys_latest_call_decides_its_state(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl", memory_limit=1)
    _call(store, "failed", "2026-10-08T10:00:00Z", category="credit")
    # A later call of the same key that succeeds clears it.
    _call(store, "finished", "2026-10-08T10:05:00Z")
    _call(
        store, "failed", "2026-10-08T10:00:00Z", provider="anthropic", category="credit"
    )
    # Arrival order and timezone spelling do not decide recency.
    _call(
        store,
        "failed",
        "2026-10-08T19:10:00+09:00",
        provider="anthropic",
        category="rate_limit",
    )
    _call(
        store,
        "finished",
        "2026-10-08T19:00:00+09:00",
        provider="anthropic",
    )
    _call(
        store,
        "failed",
        "2026-10-08T10:00:00Z",
        service="jev",
        provider="",
        category="authentication",
    )
    # A call given up on says nothing of the key.
    _call(store, "failed", "2026-10-08T11:00:00Z", service="jev", provider="")
    # Nor does a span of anything else.
    store.record(
        {
            "kind": "event",
            "type": "span.failed",
            "timestamp": "2026-10-08T12:00:00Z",
            "attributes": {"agent.kind": "cli_agent", "error.category": "credit"},
        }
    )

    failures = latest_inference_failures(store)

    assert failures.model_dump() == {
        "llm": {
            "anthropic": {
                "category": "rate_limit",
                "timestamp": "2026-10-08T19:10:00+09:00",
                "status_code": 429,
                "response": "rate_limit answer",
            }
        },
        "jev": {
            "category": "authentication",
            "timestamp": "2026-10-08T10:00:00Z",
            "status_code": 429,
            "response": "authentication answer",
        },
    }


def test_no_records_read_as_no_turns_and_no_failures(tmp_path: Path) -> None:
    empty = DiagnosticsStore(tmp_path / "empty.jsonl")
    assert last_cli_agent_turns(empty) == []
    assert latest_inference_failures(empty).model_dump() == {"llm": {}, "jev": None}


def test_device_record_endpoints_read_the_persisted_records(tmp_path: Path) -> None:
    persisted = DiagnosticsStore(tmp_path / "diag.jsonl")
    _turn(persisted, specified=True, effort="xhigh")
    _call(persisted, "failed", "2026-10-08T10:00:00Z", category="credit")
    client, _bus = _app(tmp_path)

    with client:
        turns = client.get("/intelligences/cli-agents/last-turns", headers=HEADERS)
        failures = client.get("/intelligences/inference-failures", headers=HEADERS)

    assert turns.status_code == HTTP_OK
    assert turns.json() == [
        {
            "person_id": "alice",
            "agent": "codex",
            "model": "actual",
            "model_specified": True,
            "effort": "xhigh",
            "timestamp": "2026-09-29T10:00:00Z",
        }
    ]
    assert failures.status_code == HTTP_OK
    assert failures.json() == {
        "llm": {
            "openai": {
                "category": "credit",
                "timestamp": "2026-10-08T10:00:00Z",
                "status_code": 429,
                "response": "credit answer",
            }
        },
        "jev": None,
    }
