from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.models import RuntimeStatus, RuntimeUnitStatus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.app_api.system_alerts import SystemAlertService
from guildbotics.observability.diagnostics_store import DiagnosticsStore

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}


def _runtime() -> RuntimeStatus:
    return RuntimeStatus(
        scheduler=RuntimeUnitStatus(target="scheduler", state="stopped", running=False),
        events=RuntimeUnitStatus(target="events", state="stopped", running=False),
    )


def _event(
    event_type: str,
    *,
    timestamp: str,
    trace_id: str = "trace-1",
    person_id: str = "alice",
    command: str = "workflows/demo",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "kind": "event",
        "type": event_type,
        "timestamp": timestamp,
        "trace_id": trace_id,
        "person_id": person_id,
        "command": command,
        "payload": payload or {},
    }


def test_credential_alert_opens_on_first_error_and_closes_on_success(
    tmp_path: Path,
) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    store.record(
        _event(
            "diagnostics.completed",
            timestamp="2026-07-11T10:00:00+09:00",
            payload={
                "checks": [
                    {
                        "section": "slack",
                        "code": "slack_bot_auth",
                        "status": "error",
                        "person_id": "alice",
                    }
                ]
            },
        )
    )

    alerts = SystemAlertService(store).list_alerts(_runtime()).alerts

    assert [(alert.code, alert.severity) for alert in alerts] == [
        ("credential_slack", "critical")
    ]

    store.record(
        _event(
            "diagnostics.completed",
            timestamp="2026-07-11T10:05:00+09:00",
            payload={
                "checks": [
                    {
                        "section": "slack",
                        "code": "slack_bot_auth",
                        "status": "ok",
                        "person_id": "alice",
                    }
                ]
            },
        )
    )

    assert SystemAlertService(store).list_alerts(_runtime()).alerts == []


def test_command_failure_warns_once_and_later_success_closes_it(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    store.record(_event("command.failed", timestamp="2026-07-11T10:00:00+09:00"))
    store.record(
        _event(
            "command.failed",
            timestamp="2026-07-11T10:01:00+09:00",
            trace_id="trace-2",
        )
    )

    alert = SystemAlertService(store).list_alerts(_runtime()).alerts[0]

    assert alert.code == "command_failed"
    assert alert.severity == "warning"
    assert alert.occurrence_count == 2

    store.record(
        _event(
            "command.finished",
            timestamp="2026-07-11T10:02:00+09:00",
            trace_id="trace-3",
            command="workflows/other",
        )
    )
    assert SystemAlertService(store).list_alerts(_runtime()).alerts == []


def test_unresolved_alert_survives_diagnostics_rotation_and_service_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "diagnostics.jsonl"
    store = DiagnosticsStore(path)
    store.record(_event("command.failed", timestamp="2026-07-11T10:00:00+09:00"))
    assert SystemAlertService(store).list_alerts(_runtime()).alerts

    path.write_text("", encoding="utf-8")
    restarted_store = DiagnosticsStore(path)

    alerts = SystemAlertService(restarted_store).list_alerts(_runtime()).alerts

    assert [alert.code for alert in alerts] == ["command_failed"]


def test_rate_limit_is_not_closed_by_finish_from_same_trace(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    store.record(_event("workflow.rate_limited", timestamp="2026-07-11T10:00:00+09:00"))
    store.record(_event("command.finished", timestamp="2026-07-11T10:00:01+09:00"))

    alerts = SystemAlertService(store).list_alerts(_runtime()).alerts

    assert [alert.code for alert in alerts] == ["rate_limited"]


def test_rate_limit_replaces_command_failure_from_an_earlier_trace(
    tmp_path: Path,
) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    store.record(_event("command.failed", timestamp="2026-07-11T10:00:00+09:00"))
    store.record(
        _event(
            "workflow.rate_limited",
            timestamp="2026-07-11T10:00:01+09:00",
            trace_id="trace-2",
        )
    )

    alerts = SystemAlertService(store).list_alerts(_runtime()).alerts

    assert [alert.code for alert in alerts] == ["rate_limited"]


def test_non_credential_diagnostic_error_does_not_open_credential_alert(
    tmp_path: Path,
) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    store.record(
        _event(
            "diagnostics.completed",
            timestamp="2026-07-11T10:00:00+09:00",
            payload={
                "checks": [
                    {
                        "section": "github",
                        "code": "github_lane_missing",
                        "status": "error",
                        "person_id": "alice",
                    }
                ]
            },
        )
    )

    assert SystemAlertService(store).list_alerts(_runtime()).alerts == []


def test_llm_diagnostics_open_one_workspace_credential_alert(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    store.record(
        _event(
            "diagnostics.completed",
            timestamp="2026-07-11T10:00:00+09:00",
            payload={
                "checks": [
                    {
                        "section": "llm",
                        "code": "llm_api_key",
                        "status": "error",
                        "person_id": person_id,
                    }
                    for person_id in ("alice", "bob")
                ]
            },
        )
    )
    service = SystemAlertService(store)

    alerts = service.list_alerts(_runtime()).alerts

    assert [(alert.id, alert.code, alert.person_id) for alert in alerts] == [
        ("credential:llm:", "credential_llm", "")
    ]

    store.record(
        _event(
            "diagnostics.completed",
            timestamp="2026-07-11T10:01:00+09:00",
            payload={
                "checks": [
                    {
                        "section": "llm",
                        "code": "llm_live_call",
                        "status": "ok",
                        "person_id": "alice",
                    }
                ]
            },
        )
    )

    assert service.list_alerts(_runtime()).alerts == []


def test_full_diagnostics_closes_alert_for_removed_member(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    store.record(
        _event(
            "credential.failed",
            timestamp="2026-07-11T10:00:00+09:00",
            payload={"provider": "github"},
        )
    )
    service = SystemAlertService(store)
    assert [alert.code for alert in service.list_alerts(_runtime()).alerts] == [
        "credential_github"
    ]

    store.record(
        _event(
            "diagnostics.completed",
            timestamp="2026-07-11T10:01:00+09:00",
            person_id="",
            command="",
            payload={
                "active_members": [],
                "scope_person_id": "",
                "checks": [],
            },
        )
    )

    assert service.list_alerts(_runtime()).alerts == []


def test_full_diagnostics_closes_alert_for_removed_provider(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    store.record(
        _event(
            "credential.failed",
            timestamp="2026-07-11T10:00:00+09:00",
            payload={"provider": "slack"},
        )
    )
    service = SystemAlertService(store)
    assert service.list_alerts(_runtime()).alerts

    store.record(
        _event(
            "diagnostics.completed",
            timestamp="2026-07-11T10:01:00+09:00",
            person_id="",
            command="",
            payload={
                "active_members": ["alice"],
                "scope_person_id": "",
                "checks": [
                    {
                        "section": "slack",
                        "code": "slack_not_configured",
                        "status": "ok",
                    }
                ],
            },
        )
    )

    assert service.list_alerts(_runtime()).alerts == []


def test_credential_failure_replaces_command_warning_for_same_trace(
    tmp_path: Path,
) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    store.record(_event("command.failed", timestamp="2026-07-11T10:00:00+09:00"))
    store.record(
        _event(
            "credential.failed",
            timestamp="2026-07-11T10:00:01+09:00",
            payload={"provider": "github"},
        )
    )

    alerts = SystemAlertService(store).list_alerts(_runtime()).alerts

    assert [alert.code for alert in alerts] == ["credential_github"]


def test_cli_agent_credential_failure_identifies_member_and_tool(
    tmp_path: Path,
) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    store.record(
        _event(
            "credential.failed",
            timestamp="2026-07-11T10:00:00+09:00",
            payload={"provider": "cli_agent", "cli_agent": "codex"},
        )
    )

    alerts = SystemAlertService(store).list_alerts(_runtime()).alerts

    assert len(alerts) == 1
    assert alerts[0].code == "credential_cli_agent"
    assert alerts[0].person_id == "alice"
    assert alerts[0].command == "codex"


def test_dismiss_hides_current_occurrence_and_later_failure_reopens(
    tmp_path: Path,
) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    store.record(_event("command.failed", timestamp="2026-07-11T10:00:00+09:00"))
    service = SystemAlertService(store)
    alert_id = service.list_alerts(_runtime()).alerts[0].id

    service.dismiss(alert_id)

    assert service.list_alerts(_runtime()).alerts == []
    assert SystemAlertService(store).list_alerts(_runtime()).alerts == []
    store.record(
        _event(
            "command.failed",
            timestamp="2026-07-11T10:01:00+09:00",
            trace_id="trace-2",
        )
    )
    assert [alert.code for alert in service.list_alerts(_runtime()).alerts] == [
        "command_failed"
    ]


def test_dismissed_failure_is_not_replayed_after_diagnostics_rotation(
    tmp_path: Path,
) -> None:
    store = DiagnosticsStore(
        tmp_path / "diagnostics.jsonl", memory_limit=2, max_file_bytes=1
    )
    store.record(_event("command.failed", timestamp="2026-07-11T10:00:00+09:00"))
    service = SystemAlertService(store)
    alert_id = service.list_alerts(_runtime()).alerts[0].id
    service.dismiss(alert_id)

    for index in range(3):
        store.record(
            {
                "kind": "log",
                "level": "INFO",
                "message": f"noise-{index}",
                "timestamp": "2026-07-11T10:01:00+09:00",
            }
        )

    assert service.list_alerts(_runtime()).alerts == []


def test_state_tracks_a_compact_incremental_cursor(tmp_path: Path) -> None:
    path = tmp_path / "diagnostics.jsonl"
    store = DiagnosticsStore(path)
    store.record(
        {
            "kind": "log",
            "level": "INFO",
            "message": "noise",
            "timestamp": "2026-07-11T10:00:00+09:00",
        }
    )
    store.record(_event("command.failed", timestamp="2026-07-11T10:00:01+09:00"))

    SystemAlertService(store).list_alerts(_runtime())

    state = json.loads((tmp_path / "system-alerts.json").read_text(encoding="utf-8"))
    assert state["version"] == 3
    assert state["cursor"]["offset"] == path.stat().st_size
    assert "processed" not in state


def test_hash_state_migrates_without_replaying_old_events(tmp_path: Path) -> None:
    path = tmp_path / "diagnostics.jsonl"
    store = DiagnosticsStore(path)
    store.record(_event("command.failed", timestamp="2026-07-11T10:00:00+09:00"))
    original = SystemAlertService(store).list_alerts(_runtime()).alerts[0]
    state_path = tmp_path / "system-alerts.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["version"] = 2
    state["processed"] = ["legacy-record-hash"]
    state.pop("cursor")
    state_path.write_text(json.dumps(state), encoding="utf-8")

    migrated = SystemAlertService(DiagnosticsStore(path)).list_alerts(_runtime()).alerts

    assert len(migrated) == 1
    assert migrated[0].occurrence_count == original.occurrence_count
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["version"] == 3
    assert "processed" not in saved


def test_runtime_failures_are_critical_and_state_based() -> None:
    runtime = RuntimeStatus(
        scheduler=RuntimeUnitStatus(
            target="scheduler",
            state="running",
            running=True,
            active_member_count=2,
            worker_count=1,
            started_at="2026-07-11T10:00:00+09:00",
        ),
        events=RuntimeUnitStatus(
            target="events",
            state="running",
            running=True,
            started_at="2026-07-11T10:00:00+09:00",
            events_auth_failed_persons=["alice"],
        ),
    )

    alerts = SystemAlertService(None).list_alerts(runtime).alerts

    assert [(alert.code, alert.severity) for alert in alerts] == [
        ("credential_slack", "critical")
    ]


def test_runtime_alert_dismissal_lasts_until_condition_resolves() -> None:
    failed = RuntimeStatus(
        scheduler=RuntimeUnitStatus(target="scheduler", state="stopped", running=False),
        events=RuntimeUnitStatus(
            target="events",
            state="running",
            running=True,
            events_auth_failed_persons=["alice"],
        ),
    )
    healthy = _runtime()
    service = SystemAlertService(None)
    alert_id = service.list_alerts(failed).alerts[0].id

    service.dismiss(alert_id)

    assert service.list_alerts(failed).alerts == []
    assert service.list_alerts(healthy).alerts == []
    assert [alert.code for alert in service.list_alerts(failed).alerts] == [
        "credential_slack"
    ]


def test_worker_failure_persists_until_scheduler_restarts(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    store.record(
        _event(
            "scheduler.worker.failed",
            timestamp="2026-07-11T10:00:00+09:00",
            trace_id="",
        )
    )
    service = SystemAlertService(store)

    assert [alert.code for alert in service.list_alerts(_runtime()).alerts] == [
        "worker_stopped"
    ]

    store.record(
        _event(
            "scheduler.running",
            timestamp="2026-07-11T10:01:00+09:00",
            trace_id="",
            person_id="",
            command="",
        )
    )
    assert service.list_alerts(_runtime()).alerts == []


def test_system_alerts_endpoint_reads_diagnostics(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    bus = EventBus(store=store)
    runtime = AppRuntime(bus, diagnostics_store=store)
    bus.publish_event(
        "command.failed",
        {"command": "workflows/demo", "person": "alice"},
        source="manual",
    )
    app = create_app(session_token="secret", runtime=runtime)

    with TestClient(app) as client:
        response = client.get("/system-alerts", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["alerts"][0]["code"] == "command_failed"


def test_system_alerts_endpoint_dismisses_alert(tmp_path: Path) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl")
    bus = EventBus(store=store)
    runtime = AppRuntime(bus, diagnostics_store=store)
    bus.publish_event("command.failed", {"command": "workflows/demo"})
    app = create_app(session_token="secret", runtime=runtime)

    with TestClient(app) as client:
        alert_id = client.get("/system-alerts", headers=AUTH_HEADERS).json()["alerts"][
            0
        ]["id"]
        response = client.post(
            "/system-alerts/dismiss",
            headers=AUTH_HEADERS,
            json={"alert_id": alert_id},
        )

    assert response.status_code == 200
    assert response.json() == {"alerts": []}
