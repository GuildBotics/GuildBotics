import asyncio
import os
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from yaml import safe_load

from guildbotics.app_api.api import create_app
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.models import (
    CliAgentDetectionsResponse,
    CommandRunRequest,
    ConfigStatus,
    DiagnosticCheck,
    RuntimeStatus,
    RuntimeUnitStatus,
    ScenarioDiagnosticsResponse,
    SchedulerStartRequest,
    TeamSummary,
    VerifyCheck,
    VerifyResponse,
)
from guildbotics.app_api.runtime import AppRuntime

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_UNPROCESSABLE_ENTITY = 422
HTTP_CONFLICT = 409
THREAD_WAIT_SECONDS = 2.0


def _runtime_status(
    *,
    scheduler_state: str = "stopped",
    events_state: str = "stopped",
) -> RuntimeStatus:
    return RuntimeStatus(
        scheduler=RuntimeUnitStatus(
            target="scheduler",
            state=scheduler_state,
            running=scheduler_state in {"starting", "running", "stopping"},
        ),
        events=RuntimeUnitStatus(
            target="events",
            state=events_state,
            running=events_state in {"starting", "running", "stopping"},
        ),
    )


class RuntimeStub:
    def __init__(self, tmp_path: Path) -> None:
        self.config_status = ConfigStatus(
            cwd=tmp_path,
            env_file=tmp_path / ".env",
            env_file_exists=False,
            primary_config_dir=tmp_path / ".guildbotics/config",
            primary_project_file=tmp_path / ".guildbotics/config/team/project.yml",
            primary_project_file_exists=False,
            home_config_dir=tmp_path / "home/.guildbotics/config",
            home_project_file=tmp_path / "home/.guildbotics/config/team/project.yml",
            home_project_file_exists=False,
            storage_dir=tmp_path / "home/.guildbotics/data",
        )

    def stop_scheduler(self) -> RuntimeStatus:
        return _runtime_status()

    def get_config_status(self) -> ConfigStatus:
        return self.config_status

    def set_workspace(self, workspace_dir: Path) -> ConfigStatus:
        project_file = workspace_dir / ".guildbotics/config/team/project.yml"
        self.config_status = self.config_status.model_copy(
            update={
                "cwd": workspace_dir,
                "env_file": workspace_dir / ".env",
                "env_file_exists": (workspace_dir / ".env").exists(),
                "primary_config_dir": workspace_dir / ".guildbotics/config",
                "primary_project_file": project_file,
                "primary_project_file_exists": project_file.exists(),
            }
        )
        return self.config_status

    def get_team_summary(self) -> TeamSummary:
        return TeamSummary(
            project={
                "name": "GuildBotics",
                "language_code": "en",
                "language_name": "English",
            },
            members=[],
        )

    async def run_command(self, request):
        if request.command == "missing":
            raise AppApiError(
                "command_error",
                "Unable to locate command 'missing'.",
                context={"command": request.command},
            )
        return {"request_id": "stub-request", "output": f"ran {request.command}"}

    def get_scheduler_status(self) -> RuntimeStatus:
        return _runtime_status()

    def start_scheduler(self, request) -> RuntimeStatus:
        if request.only == "events":
            return _runtime_status(events_state="running")
        return _runtime_status(scheduler_state="running", events_state="running")

    def get_default_routines(self) -> list[str]:
        return ["workflows/ticket_driven_workflow"]

    def requires_github_for_routine(self, command: str) -> bool:
        return command == "workflows/ticket_driven_workflow"

    def verify(self) -> VerifyResponse:
        return VerifyResponse(
            ok=False,
            config=self.config_status,
            active_members=[],
            checks=[
                VerifyCheck(
                    code="active_members",
                    status="error",
                    message="No active members are configured.",
                )
            ],
            warnings=[],
            errors=[
                VerifyCheck(
                    code="active_members",
                    status="error",
                    message="No active members are configured.",
                )
            ],
        )

    async def run_scenario_diagnostics(
        self, person_id: str | None = None
    ) -> ScenarioDiagnosticsResponse:
        return ScenarioDiagnosticsResponse(
            ok=False,
            active_members=[person_id] if person_id else [],
            checks=[
                DiagnosticCheck(
                    section="members",
                    code="active_members",
                    status="error",
                    message="No active members are configured.",
                )
            ],
            warnings=[],
            errors=[
                DiagnosticCheck(
                    section="members",
                    code="active_members",
                    status="error",
                    message="No active members are configured.",
                )
            ],
        )

    def detect_cli_agents(self) -> CliAgentDetectionsResponse:
        return CliAgentDetectionsResponse(
            agents=[
                {
                    "name": "claude",
                    "executable": "claude",
                    "detected": True,
                    "path": "/usr/local/bin/claude",
                },
                {
                    "name": "codex",
                    "executable": "codex",
                    "detected": False,
                    "path": "",
                },
            ]
        )

    def is_github_integration_enabled(self) -> bool:
        return False


def test_health_requires_session_token(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        unauthorized = client.get("/health")
        response = client.get(
            "/health", headers={"X-GuildBotics-Session-Token": "secret"}
        )

    assert unauthorized.status_code == HTTP_UNAUTHORIZED
    assert unauthorized.json() == {
        "code": "invalid_session_token",
        "message": "Invalid session token.",
        "context": {},
    }
    assert response.status_code == HTTP_OK
    assert response.json() == {"status": "ok"}


def test_workspace_change_updates_runtime_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/workspace",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={"workspace_dir": str(workspace)},
        )

    assert response.status_code == HTTP_OK
    assert response.json()["cwd"] == str(workspace)
    assert response.json()["primary_config_dir"] == str(
        workspace / ".guildbotics/config"
    )


def test_command_run_uses_runtime(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/commands/run",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={"command": "hello"},
        )

    assert response.status_code == HTTP_OK
    assert response.json() == {
        "request_id": "stub-request",
        "output": "ran hello",
    }


def test_event_stream_replays_request_id(tmp_path: Path) -> None:
    event_bus = EventBus()
    app = create_app(
        session_token="secret",
        runtime=RuntimeStub(tmp_path),
        event_bus=event_bus,
    )
    event_bus.publish_event(
        "command.started",
        {"command": "hello"},
        request_id="request-1",
    )

    with (
        TestClient(app) as client,
        client.websocket_connect("/events?token=secret") as websocket,
    ):
        event = websocket.receive_json()

    assert event["type"] == "command.started"
    assert event["request_id"] == "request-1"
    assert event["payload"] == {"command": "hello"}
    assert event["timestamp"]


def test_command_error_uses_stable_error_shape(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/commands/run",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={"command": "missing"},
        )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json() == {
        "code": "command_error",
        "message": "Unable to locate command 'missing'.",
        "context": {"command": "missing"},
    }


def test_scheduler_start_only_events_uses_runtime(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/scheduler/start",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={"only": "events"},
        )

    assert response.status_code == HTTP_OK
    assert response.json()["events"]["state"] == "running"
    assert response.json()["scheduler"]["state"] == "stopped"


def test_validation_error_uses_stable_error_shape(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/scheduler/start",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={"only": "invalid-value"},
        )

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    payload = response.json()
    assert payload["code"] == "validation_error"
    assert payload["message"] == "Request validation failed."
    assert isinstance(payload["context"].get("errors"), list)


def test_cli_agent_detection_endpoint_uses_runtime(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/intelligences/cli-agents/detection",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["agents"][0]["name"] == "claude"
    assert payload["agents"][0]["detected"] is True
    assert payload["agents"][1]["name"] == "codex"
    assert payload["agents"][1]["detected"] is False


def test_scenario_diagnostics_endpoint_uses_runtime(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/diagnostics/scenario",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["ok"] is False
    assert payload["errors"][0]["section"] == "members"
    assert payload["errors"][0]["code"] == "active_members"


def test_scenario_diagnostics_endpoint_accepts_person_id(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/diagnostics/scenario?person_id=alice",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )

    assert response.status_code == HTTP_OK
    assert response.json()["active_members"] == ["alice"]


def test_config_init_endpoint_writes_project_without_github(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))
    config_dir = tmp_path / ".guildbotics/config"
    env_file_path = tmp_path / ".env"

    with TestClient(app) as client:
        response = client.post(
            "/config/init",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "env_file_path": str(env_file_path),
                "env_file_option": "overwrite",
                "language": "en",
                "description": "Local automation workspace",
                "llm_api_type": "openai",
                "cli_agent": "codex",
                "openai_api_key": "test-openai-key",
            },
        )

    assert response.status_code == HTTP_OK
    assert (config_dir / "team/project.yml").exists()
    assert "test-openai-key" not in response.text
    assert "OPENAI_API_KEY=test-openai-key" in env_file_path.read_text()


def test_config_project_endpoints_read_and_update_non_destructively(
    tmp_path: Path,
) -> None:
    runtime = RuntimeStub(tmp_path)
    app = create_app(session_token="secret", runtime=runtime)
    config_dir = tmp_path / ".guildbotics/config"
    team_dir = config_dir / "team"
    team_dir.mkdir(parents=True, exist_ok=True)
    (team_dir / "project.yml").write_text(
        "\n".join(
            [
                "language: en",
                "description: Existing description",
                "repositories:",
                "  - name: GuildBotics",
                "services:",
                "  ticket_manager:",
                "    name: GitHub",
                "    owner: GuildBotics",
                "    project_id: '7'",
                "    url: https://github.com/orgs/GuildBotics/projects/7",
                "  code_hosting_service:",
                "    name: GitHub",
                "    owner: GuildBotics",
                "    repo_base_url: https://github.com",
            ]
        )
    )
    runtime.config_status.primary_project_file_exists = True
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=existing-openai\nEXTRA=keep")
    model_mapping = config_dir / "intelligences/model_mapping.yml"
    model_mapping.parent.mkdir(parents=True, exist_ok=True)
    model_mapping.write_text(
        "\n".join(
            [
                "default: models/openai/gpt-5-mini.yml",
                "openai: models/openai/gpt-5-mini.yml",
                "gemini: models/gemini/gemini-3-flash.yml",
                "anthropic: models/anthropic/claude-haiku-4.yml",
            ]
        )
    )
    cli_mapping = config_dir / "intelligences/cli_agent_mapping.yml"
    cli_mapping.write_text(
        "\n".join(
            [
                "default: claude-cli.yml",
                "codex: codex-cli.yml",
                "gemini: gemini-cli.yml",
                "claude: claude-cli.yml",
                "copilot: copilot-cli.yml",
            ]
        )
    )

    with TestClient(app) as client:
        get_response = client.get(
            "/config/project",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )
        assert get_response.status_code == HTTP_OK
        payload = get_response.json()
        assert payload["language"] == "en"
        assert payload["llm_api_type"] == "openai"
        assert payload["cli_agent"] == "claude"
        assert payload["has_openai_api_key"] is True
        assert payload["has_google_api_key"] is False

        put_response = client.put(
            "/config/project",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "env_file_path": str(env_file),
                "language": "ja",
                "description": "Updated description",
                "llm_api_type": "gemini",
                "cli_agent": "codex",
                "github_enabled": True,
                "repository_name": "GuildBotics",
                "owner": "GuildBotics",
                "project_id": "7",
                "github_project_url": "https://github.com/orgs/GuildBotics/projects/7",
                "repo_base_url": "ssh://git@github.com",
            },
        )
        assert put_response.status_code == HTTP_OK

    updated_project = safe_load((team_dir / "project.yml").read_text())
    assert updated_project["language"] == "ja"
    assert updated_project["description"] == "Updated description"
    assert (
        updated_project["services"]["code_hosting_service"]["repo_base_url"]
        == "ssh://git@github.com"
    )

    env_text = env_file.read_text()
    assert "OPENAI_API_KEY=existing-openai" in env_text
    assert "EXTRA=keep" in env_text


def test_config_members_resolve_endpoint(tmp_path: Path, monkeypatch) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    class ReferenceStub:
        def model_dump(self) -> dict[str, Any]:
            return {
                "person_id": "alice",
                "github_username": "alice",
                "github_user_id": 123,
                "git_email": "123+alice@users.noreply.github.com",
            }

    monkeypatch.setattr(
        "guildbotics.app_api.api.SimplePersonSetupService.resolve_github_user",
        lambda self, identity, is_github_apps=False: ReferenceStub(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/config/members/resolve",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={"person_type": "machine_user", "identity": "alice"},
        )

    assert response.status_code == HTTP_OK
    assert response.json() == {
        "person_id": "alice",
        "github_username": "alice",
        "github_user_id": 123,
        "git_email": "123+alice@users.noreply.github.com",
    }


def test_scheduler_routines_endpoint(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/scheduler/routines",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["routines"] == [
        {
            "command": "workflows/ticket_driven_workflow",
            "requires_github": True,
        }
    ]


def test_roles_endpoint_returns_template_roles(tmp_path: Path) -> None:
    app = create_app(session_token="secret", runtime=RuntimeStub(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/config/roles?language=ja",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert isinstance(payload["roles"], list)
    role_ids = [role["role_id"] for role in payload["roles"]]
    assert "architect" in role_ids


def test_intelligence_config_endpoints_read_update_and_member_inherit(
    tmp_path: Path,
) -> None:
    runtime = RuntimeStub(tmp_path)
    app = create_app(session_token="secret", runtime=runtime)
    config_dir = tmp_path / ".guildbotics/config"
    (config_dir / "team").mkdir(parents=True, exist_ok=True)
    (config_dir / "team/project.yml").write_text("language: en")
    runtime.config_status.primary_project_file_exists = True

    with TestClient(app) as client:
        get_response = client.get(
            "/config/intelligences",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )
        assert get_response.status_code == HTTP_OK
        payload = get_response.json()
        assert payload["model_mapping"]["default"].startswith("models/")
        assert payload["brain_mapping"][0]["name"] == "default"
        assert any(agent["path"] == "codex-cli.yml" for agent in payload["cli_agents"])

        payload["model_mapping"]["default"] = "models/openai/gpt-5-mini.yml"
        payload["brain_mapping"][0] = {
            "name": "default",
            "brain_class": "guildbotics.intelligences.brains.cli_agent.CliAgentBrain",
            "engine": "cli",
            "target": "codex",
        }
        update_response = client.put(
            "/config/intelligences",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "model_mapping": payload["model_mapping"],
                "models": payload["models"],
                "cli_agent_mapping": payload["cli_agent_mapping"],
                "cli_agents": payload["cli_agents"],
                "brain_mapping": payload["brain_mapping"],
            },
        )
        assert update_response.status_code == HTTP_OK

        member_response = client.get(
            "/config/intelligences?person_id=alice",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )
        assert member_response.status_code == HTTP_OK
        member_payload = member_response.json()
        assert member_payload["inherited"] is True
        assert (
            member_payload["model_mapping"]["default"] == "models/openai/gpt-5-mini.yml"
        )

        member_payload["brain_mapping"][0] = {
            "name": "default",
            "brain_class": "guildbotics.intelligences.brains.agno_agent.AgnoAgentDefaultBrain",
            "engine": "llm",
            "target": "anthropic",
        }
        member_update = client.put(
            "/config/intelligences",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "person_id": "alice",
                "model_mapping": member_payload["model_mapping"],
                "models": member_payload["models"],
                "cli_agent_mapping": member_payload["cli_agent_mapping"],
                "cli_agents": member_payload["cli_agents"],
                "brain_mapping": member_payload["brain_mapping"],
            },
        )
        assert member_update.status_code == HTTP_OK
        member_intelligences_dir = config_dir / "team/members/alice/intelligences"
        assert (
            safe_load((member_intelligences_dir / "model_mapping.yml").read_text())
            == member_payload["model_mapping"]
        )
        assert (
            safe_load((member_intelligences_dir / "cli_agent_mapping.yml").read_text())
            == member_payload["cli_agent_mapping"]
        )
        assert sorted(
            path.relative_to(member_intelligences_dir).as_posix()
            for path in member_intelligences_dir.rglob("*")
            if path.is_file()
        ) == ["cli_agent_mapping.yml", "model_mapping.yml"]

        inherit_reset = client.put(
            "/config/intelligences",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "person_id": "alice",
                "inherit_team_defaults": True,
            },
        )
        assert inherit_reset.status_code == HTTP_OK

    brain_mapping = safe_load(
        (config_dir / "intelligences/brain_mapping.yml").read_text()
    )
    assert brain_mapping["default"]["class"] == (
        "guildbotics.intelligences.brains.cli_agent.CliAgentBrain"
    )
    assert not (config_dir / "team/members/alice/intelligences").exists()


def test_member_config_endpoints_read_update_delete(tmp_path: Path) -> None:
    runtime = RuntimeStub(tmp_path)
    app = create_app(session_token="secret", runtime=runtime)
    config_dir = tmp_path / ".guildbotics/config"
    member_dir = config_dir / "team/members/alice"
    member_dir.mkdir(parents=True, exist_ok=True)
    (member_dir / "person.yml").write_text(
        "\n".join(
            [
                "person_id: alice",
                "name: Alice Bot",
                "is_active: true",
                "person_type: machine_user",
                "account_info:",
                "  github_username: alice",
                "  git_user: Alice Bot",
                "  git_email: 123+alice@users.noreply.github.com",
                "profile:",
                "  professional:",
                "    architect: {}",
                "  personal: {}",
                "  programmer: {}",
                "  character:",
                "    archetype: strategic_project_manager_architect",
                "    traits:",
                "      - strategic",
                "speaking_style: concise",
                "relationships: team lead",
                "message_channels:",
                "  - name: C012345",
                "    service: slack",
            ]
        )
    )
    team_dir = config_dir / "team"
    (team_dir / "project.yml").parent.mkdir(parents=True, exist_ok=True)
    (team_dir / "project.yml").write_text("language: en")
    runtime.config_status.primary_project_file_exists = True
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ALICE_GITHUB_ACCESS_TOKEN=token-a",
                "ALICE_SLACK_BOT_TOKEN=xoxb-a",
                "ALICE_SLACK_APP_TOKEN=xapp-a",
            ]
        )
    )

    with TestClient(app) as client:
        get_response = client.get(
            "/config/members/alice",
            headers={"X-GuildBotics-Session-Token": "secret"},
        )
        assert get_response.status_code == HTTP_OK
        payload = get_response.json()
        assert payload["person_id"] == "alice"
        assert payload["roles"] == ["architect"]
        assert (
            payload["character"]["archetype"] == "strategic_project_manager_architect"
        )
        assert payload["has_github_access_token"] is True
        assert payload["has_slack_bot_token"] is True
        assert payload["slack_channels"] == ["C012345"]

        put_response = client.put(
            "/config/members/alice",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "env_file_path": str(env_file),
                "original_person_id": "alice",
                "person_type": "machine_user",
                "person_id": "alice-renamed",
                "person_name": "Alice Updated",
                "is_active": False,
                "github_username": "alice-renamed",
                "git_email": "123+alice-renamed@users.noreply.github.com",
                "roles": ["reviewer"],
                "speaking_style": "updated",
                "relationships": "updated",
                "character": {
                    "archetype": "creative_designer",
                    "traits": ["creative", "playful"],
                    "interests": ["anime"],
                    "conversation_preferences": {
                        "join_when": ["ux discussion"],
                        "avoid_when": ["off-topic"],
                        "contribution_style": ["user perspective"],
                    },
                },
                "github_access_token": "token-b",
                "slack_bot_token": "xoxb-b",
                "slack_app_token": "xapp-b",
                "slack_channels": ["C0999"],
            },
        )
        assert put_response.status_code == HTTP_OK

        renamed_file = config_dir / "team/members/alice-renamed/person.yml"
        updated = safe_load(renamed_file.read_text())
        assert updated["person_id"] == "alice-renamed"
        assert updated["name"] == "Alice Updated"
        assert "reviewer" in updated["profile"]["professional"]
        assert updated["profile"]["character"]["archetype"] == "creative_designer"
        assert updated["message_channels"][0]["name"] == "C0999"
        env_text = env_file.read_text()
        assert "ALICE_GITHUB_ACCESS_TOKEN" not in env_text
        assert "ALICE_RENAMED_GITHUB_ACCESS_TOKEN=token-b" in env_text
        assert "ALICE_RENAMED_SLACK_BOT_TOKEN=xoxb-b" in env_text

        delete_response = client.request(
            "DELETE",
            "/config/members/alice-renamed",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "env_file_path": str(env_file),
            },
        )
        assert delete_response.status_code == HTTP_OK
    assert not (config_dir / "team/members/alice-renamed/person.yml").exists()
    env_text_after_delete = env_file.read_text()
    assert "ALICE_RENAMED_GITHUB_ACCESS_TOKEN=token-b" not in env_text_after_delete
    assert "ALICE_RENAMED_SLACK_BOT_TOKEN" not in env_text_after_delete


def test_member_create_uses_existing_runtime_config_dir(tmp_path: Path) -> None:
    runtime = RuntimeStub(tmp_path)
    app = create_app(session_token="secret", runtime=runtime)
    config_dir = tmp_path / ".guildbotics/config"
    (config_dir / "team").mkdir(parents=True, exist_ok=True)
    (config_dir / "team/project.yml").write_text("language: en")
    runtime.config_status.primary_project_file_exists = True

    wrong_config_dir = tmp_path / "wrong/.guildbotics/config"
    wrong_env_file = tmp_path / "wrong/.env"
    with TestClient(app) as client:
        response = client.post(
            "/config/members",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(wrong_config_dir),
                "env_file_path": str(wrong_env_file),
                "person_type": "",
                "person_id": "new_member",
                "person_name": "New Member",
                "is_active": True,
                "github_username": "",
                "git_email": "",
                "roles": ["architect"],
            },
        )
    assert response.status_code == HTTP_OK
    assert (config_dir / "team/members/new_member/person.yml").exists()
    assert not (wrong_config_dir / "team/members/new_member/person.yml").exists()


def test_member_config_accepts_member_without_github_link(tmp_path: Path) -> None:
    runtime = RuntimeStub(tmp_path)
    app = create_app(session_token="secret", runtime=runtime)
    config_dir = tmp_path / ".guildbotics/config"
    env_file = tmp_path / ".env"

    with TestClient(app) as client:
        response = client.post(
            "/config/members",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={
                "config_dir": str(config_dir),
                "env_file_path": str(env_file),
                "person_type": "",
                "person_id": "local-agent",
                "person_name": "Local Agent",
                "is_active": True,
                "github_username": "",
                "git_email": "",
                "roles": ["architect"],
                "speaking_style": "concise",
            },
        )

    assert response.status_code == HTTP_OK
    person_config = safe_load(
        (config_dir / "team/members/local-agent/person.yml").read_text()
    )
    assert person_config["person_id"] == "local-agent"
    assert "person_type" not in person_config
    assert person_config["account_info"] == {"git_user": "Local Agent"}
    assert not env_file.exists()


def test_app_runtime_reports_missing_config(monkeypatch) -> None:
    class MissingConfigSetupTool:
        def get_context(self, message: str = ""):
            raise FileNotFoundError(2, "No such file", "project.yml")

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_setup_tool",
        lambda: MissingConfigSetupTool(),
    )

    runtime = AppRuntime(EventBus())

    with pytest.raises(AppApiError) as exc_info:
        runtime.get_team_summary()

    assert exc_info.value.code == "config_not_found"
    assert exc_info.value.context == {"path": "project.yml"}


def test_app_runtime_reload_workspace_env_before_context(monkeypatch, tmp_path) -> None:
    class ProjectStub:
        name = "Project"

        def get_language_code(self) -> str:
            return "ja"

        def get_language_name(self) -> str:
            return "日本語"

    class ContextStub:
        team = type(
            "TeamStub",
            (),
            {
                "project": ProjectStub(),
                "members": [],
            },
        )()

    class SetupToolStub:
        def get_context(self, message: str = "") -> object:
            assert os.environ["OPENAI_API_KEY"] == "new-key"
            return ContextStub()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "old-key")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=new-key\n")
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_setup_tool",
        lambda: SetupToolStub(),
    )

    runtime = AppRuntime(EventBus())

    runtime.get_team_summary()


def test_app_runtime_scheduler_start_stop_lifecycle(monkeypatch) -> None:
    class SetupToolStub:
        def get_context(self, message: str = "") -> object:
            return object()

        def get_default_routines(self) -> list[str]:
            return ["routine"]

    default_stop_timeout = 10.0
    started = threading.Event()
    release = threading.Event()

    class BlockingScheduler:
        instances: ClassVar[list["BlockingScheduler"]] = []

        def __init__(
            self,
            context: object,
            routine_commands: list[str],
            consecutive_error_limit: int,
        ) -> None:
            self.shutdown_calls = 0
            self.routine_commands = routine_commands
            self.consecutive_error_limit = consecutive_error_limit
            BlockingScheduler.instances.append(self)

        def start(self) -> None:
            started.set()
            release.wait(THREAD_WAIT_SECONDS)

        def shutdown(
            self, graceful: bool = True, timeout: float | None = None
        ) -> None:
            self.shutdown_calls += 1
            self.shutdown_timeout = timeout
            release.set()

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_setup_tool",
        lambda: SetupToolStub(),
    )
    monkeypatch.setattr(
        "guildbotics.app_api.lifecycle.TaskScheduler",
        BlockingScheduler,
    )

    runtime = AppRuntime(EventBus(), stop_timeout_seconds=default_stop_timeout)

    first = runtime.start_scheduler(SchedulerStartRequest(only="scheduler"))
    assert first.scheduler.state == "running"
    assert started.wait(THREAD_WAIT_SECONDS)

    second = runtime.start_scheduler(SchedulerStartRequest(only="scheduler"))
    assert second.scheduler.state == "running"
    assert len(BlockingScheduler.instances) == 1

    stopped = runtime.stop_scheduler()
    assert stopped.scheduler.state == "stopped"
    assert BlockingScheduler.instances[0].shutdown_calls == 1
    assert BlockingScheduler.instances[0].shutdown_timeout == default_stop_timeout

    stopped_again = runtime.stop_scheduler()
    assert stopped_again.scheduler.state == "stopped"
    assert BlockingScheduler.instances[0].shutdown_calls == 1


def test_app_runtime_marks_scheduler_failed_on_stop_timeout(monkeypatch) -> None:
    class SetupToolStub:
        def get_context(self, message: str = "") -> object:
            return object()

        def get_default_routines(self) -> list[str]:
            return ["routine"]

    started = threading.Event()
    release = threading.Event()

    class StuckScheduler:
        def __init__(
            self,
            context: object,
            routine_commands: list[str],
            consecutive_error_limit: int,
        ) -> None:
            self.shutdown_timeout: float | None = None

        def start(self) -> None:
            started.set()
            release.wait(THREAD_WAIT_SECONDS)

        def shutdown(
            self, graceful: bool = True, timeout: float | None = None
        ) -> None:
            self.shutdown_timeout = timeout

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_setup_tool",
        lambda: SetupToolStub(),
    )
    monkeypatch.setattr(
        "guildbotics.app_api.lifecycle.TaskScheduler",
        StuckScheduler,
    )

    runtime = AppRuntime(EventBus(), stop_timeout_seconds=0.01)

    first = runtime.start_scheduler(SchedulerStartRequest(only="scheduler"))
    assert first.scheduler.state == "running"
    assert started.wait(THREAD_WAIT_SECONDS)

    stopped = runtime.stop_scheduler()
    assert stopped.scheduler.state == "failed"
    assert stopped.scheduler.running is True
    assert stopped.scheduler.error == "Scheduler did not stop before timeout."
    release.set()


def test_app_runtime_event_listener_start_stop_lifecycle(monkeypatch) -> None:
    class SetupToolStub:
        def get_context(self, message: str = "") -> object:
            return object()

        def get_default_routines(self) -> list[str]:
            return ["routine"]

    class RunningEventListener:
        instances: ClassVar[list["RunningEventListener"]] = []

        def __init__(self, context: object) -> None:
            self.alive = False
            self.stop_calls = 0
            RunningEventListener.instances.append(self)

        def start(self) -> None:
            self.alive = True

        def stop(self) -> None:
            self.stop_calls += 1
            self.alive = False

        def join(self, timeout: float | None = None) -> None:
            return

        def is_alive(self) -> bool:
            return self.alive

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_setup_tool",
        lambda: SetupToolStub(),
    )
    monkeypatch.setattr(
        "guildbotics.app_api.lifecycle.EventListenerRunner",
        RunningEventListener,
    )

    runtime = AppRuntime(EventBus())

    first = runtime.start_scheduler(SchedulerStartRequest(only="events"))
    assert first.events.state == "running"

    second = runtime.start_scheduler(SchedulerStartRequest(only="events"))
    assert second.events.state == "running"
    assert len(RunningEventListener.instances) == 1

    stopped = runtime.stop_scheduler()
    assert stopped.events.state == "stopped"
    assert RunningEventListener.instances[0].stop_calls == 1

    stopped_again = runtime.stop_scheduler()
    assert stopped_again.events.state == "stopped"
    assert RunningEventListener.instances[0].stop_calls == 1


def test_app_runtime_marks_event_listener_failed_on_start_error(monkeypatch) -> None:
    class MissingConfigSetupTool:
        def get_context(self, message: str = "") -> object:
            raise FileNotFoundError(2, "No such file", "project.yml")

        def get_default_routines(self) -> list[str]:
            return ["routine"]

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_setup_tool",
        lambda: MissingConfigSetupTool(),
    )

    runtime = AppRuntime(EventBus())

    with pytest.raises(AppApiError) as exc_info:
        runtime.start_scheduler(SchedulerStartRequest(only="events"))

    assert exc_info.value.code == "config_not_found"
    assert runtime.get_scheduler_status().events.state == "failed"


def test_app_runtime_marks_event_listener_failed_on_stop_timeout(monkeypatch) -> None:
    class SetupToolStub:
        def get_context(self, message: str = "") -> object:
            return object()

        def get_default_routines(self) -> list[str]:
            return ["routine"]

    class StuckEventListener:
        def __init__(self, context: object) -> None:
            self.alive = False

        def start(self) -> None:
            self.alive = True

        def stop(self) -> None:
            return

        def join(self, timeout: float | None = None) -> None:
            return

        def is_alive(self) -> bool:
            return self.alive

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_setup_tool",
        lambda: SetupToolStub(),
    )
    monkeypatch.setattr(
        "guildbotics.app_api.lifecycle.EventListenerRunner",
        StuckEventListener,
    )

    runtime = AppRuntime(EventBus(), stop_timeout_seconds=0.01)

    started = runtime.start_scheduler(SchedulerStartRequest(only="events"))
    assert started.events.state == "running"

    stopped = runtime.stop_scheduler()
    assert stopped.events.state == "failed"
    assert stopped.events.running is True
    assert stopped.events.error == "Event listener runner did not stop before timeout."


def test_app_runtime_rejects_github_required_routine_without_integration() -> None:
    runtime = AppRuntime(EventBus())
    runtime.is_github_integration_enabled = lambda: False  # type: ignore[method-assign]
    runtime.requires_github_for_routine = lambda command: True  # type: ignore[method-assign]

    with pytest.raises(AppApiError) as exc_info:
        runtime.start_scheduler(
            SchedulerStartRequest(routine_commands=["workflows/ticket_driven_workflow"])
        )

    assert exc_info.value.code == "github_integration_required_for_routine"


@pytest.mark.asyncio
async def test_app_runtime_rejects_parallel_commands(monkeypatch) -> None:
    event_bus = EventBus()
    runtime = AppRuntime(event_bus)
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_command(*_: Any, **__: Any) -> str:
        started.set()
        await release.wait()
        return "done"

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

    running = asyncio.create_task(
        runtime.run_command(CommandRunRequest(command="first"))
    )
    await started.wait()

    with pytest.raises(AppApiError) as exc_info:
        await runtime.run_command(CommandRunRequest(command="second"))

    release.set()
    response = await running

    assert response.output == "done"
    assert exc_info.value.code == "command_already_running"
    assert exc_info.value.status_code == HTTP_CONFLICT
    events = event_bus.snapshot_events()
    assert [event["type"] for event in events] == [
        "command.started",
        "command.finished",
    ]
    assert all(event["request_id"] == response.request_id for event in events)
