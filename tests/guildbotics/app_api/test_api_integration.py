"""End-to-end integration tests against a real temporary workspace.

Unlike ``test_api.py`` (which drives a stubbed runtime), these tests build a
real :class:`AppRuntime` whose working directory is a ``tmp_path`` workspace.
They exercise the FastAPI app via ``TestClient`` and assert that requests
actually create / read / update files on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from yaml import safe_load

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime

HTTP_OK = 200

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A hermetic temp workspace acting as the runtime working directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    monkeypatch.delenv("GUILDBOTICS_TRANSCRIPT_DETAIL", raising=False)
    monkeypatch.delenv("GUILDBOTICS_TRANSCRIPT_RETENTION_DAYS", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def client(workspace: Path) -> TestClient:
    """A TestClient bound to a real runtime rooted at the temp workspace."""
    app = create_app(session_token="secret", runtime=AppRuntime(EventBus()))
    return TestClient(app)


def _init_project(
    client: TestClient,
    config_dir: Path,
    env_file: Path,
    *,
    language: str = "en",
    description: str = "Temp automation workspace",
) -> None:
    response = client.post(
        "/config/init",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(config_dir),
            "env_file_path": str(env_file),
            "env_file_option": "overwrite",
            "language": language,
            "description": description,
            "llm_api_type": "openai",
            "cli_agent": "codex",
            "provider_api_keys": {"openai": "test-openai-key"},
        },
    )
    assert response.status_code == HTTP_OK


def test_temp_workspace_init_project_member_team_flow(
    client: TestClient, workspace: Path
) -> None:
    config_dir = workspace / ".guildbotics/config"
    env_file = workspace / ".env"

    with client:
        _init_project(client, config_dir, env_file)
        # config/init wrote the project file on disk.
        assert (config_dir / "team/project.yml").exists()

        project = client.get("/config/project", headers=AUTH_HEADERS)
        assert project.status_code == HTTP_OK
        project_payload = project.json()
        assert project_payload["language"] == "en"
        assert project_payload["description"] == "Temp automation workspace"
        assert project_payload["llm_api_type"] == "openai"
        assert project_payload["provider_api_keys"]["openai"] is True
        assert project_payload["github_enabled"] is False

        member = client.post(
            "/config/members",
            headers=AUTH_HEADERS,
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
        assert member.status_code == HTTP_OK
        assert (config_dir / "team/members/local-agent/person.yml").exists()

        team = client.get("/team", headers=AUTH_HEADERS)

    assert team.status_code == HTTP_OK
    team_payload = team.json()
    assert team_payload["project"]["language_code"] == "en"
    members = team_payload["members"]
    assert [entry["person_id"] for entry in members] == ["local-agent"]
    assert members[0]["name"] == "Local Agent"
    assert members[0]["is_active"] is True


def test_temp_workspace_command_options_return_localized_sample_command(
    client: TestClient, workspace: Path
) -> None:
    config_dir = workspace / ".guildbotics/config"
    env_file = workspace / ".env"

    with client:
        _init_project(client, config_dir, env_file, language="ja")
        response = client.get("/commands/options", headers=AUTH_HEADERS)

    assert response.status_code == HTTP_OK
    options = {option["command"]: option for option in response.json()["options"]}
    # Sample commands were seeded into the otherwise-empty workspace.
    assert {"translate", "summarize", "get-time-of-day", "context-info"} <= set(options)
    # The seeded markdown file actually exists on disk.
    assert (config_dir / "commands/translate.md").exists()
    # The ja language code resolves the localized (.ja) description, not the
    # English fallback, for the sample command.
    translate = options["translate"]
    assert translate["label"] == "Translate"
    assert translate["description"] == (
        "入力文をOSのUI言語と英語の間で相互翻訳します。"
        "OSのUI言語が英語の場合は日本語を使用します。"
    )
    assert translate["arguments"] == []
    assert translate["inputs"] == {
        "defined_args": "auto",
        "extra_args": "hidden",
        "message": "required",
    }


def test_temp_workspace_intelligence_update_writes_files(
    client: TestClient, workspace: Path
) -> None:
    config_dir = workspace / ".guildbotics/config"
    env_file = workspace / ".env"

    with client:
        _init_project(client, config_dir, env_file)

        current = client.get("/config/intelligences", headers=AUTH_HEADERS)
        assert current.status_code == HTTP_OK
        payload = current.json()
        payload["model_mapping"]["default"] = "models/openai/gpt-5-mini.yml"

        update = client.put(
            "/config/intelligences",
            headers=AUTH_HEADERS,
            json={
                "config_dir": str(config_dir),
                "model_mapping": payload["model_mapping"],
                "models": payload["models"],
                "cli_agent_mapping": payload["cli_agent_mapping"],
                "cli_agents": payload["cli_agents"],
                "brain_mapping": payload["brain_mapping"],
            },
        )

    assert update.status_code == HTTP_OK
    written_files = {entry["path"] for entry in update.json()["intelligence"]["files"]}
    model_mapping_file = config_dir / "intelligences/model_mapping.yml"
    assert str(model_mapping_file) in written_files
    # The file on disk reflects the requested change.
    assert model_mapping_file.exists()
    assert (
        safe_load(model_mapping_file.read_text())["default"]
        == "models/openai/gpt-5-mini.yml"
    )
    assert (config_dir / "intelligences/cli_agent_mapping.yml").exists()
    assert (config_dir / "intelligences/brain_mapping.yml").exists()


def test_temp_workspace_transcript_settings_update_then_status(
    client: TestClient, workspace: Path
) -> None:
    config_dir = workspace / ".guildbotics/config"
    env_file = workspace / ".env"

    with client:
        _init_project(client, config_dir, env_file)

        update = client.put(
            "/transcripts/settings",
            headers=AUTH_HEADERS,
            json={"detail": "full", "retention_days": 14},
        )
        assert update.status_code == HTTP_OK
        assert update.json()["detail"] == "full"
        assert update.json()["retention_days"] == 14

        status = client.get("/transcripts/settings", headers=AUTH_HEADERS)

    assert status.status_code == HTTP_OK
    status_payload = status.json()
    assert status_payload["detail"] == "full"
    assert status_payload["retention_days"] == 14
    env_text = env_file.read_text()
    assert "GUILDBOTICS_TRANSCRIPT_DETAIL=full" in env_text
    assert "GUILDBOTICS_TRANSCRIPT_RETENTION_DAYS=14" in env_text
