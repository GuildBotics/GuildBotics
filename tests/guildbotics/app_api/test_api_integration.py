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
HTTP_UNPROCESSABLE_ENTITY = 422

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


def test_temp_workspace_command_files_crud_flow(
    client: TestClient, workspace: Path
) -> None:
    config_dir = workspace / ".guildbotics/config"
    env_file = workspace / ".env"

    with client:
        _init_project(client, config_dir, env_file, language="en")

        listing = client.get("/commands/files", headers=AUTH_HEADERS)
        assert listing.status_code == HTTP_OK
        commands = {item["command"] for item in listing.json()["files"]}
        assert {"translate", "summarize"} <= commands

        summary = next(
            item for item in listing.json()["files"] if item["command"] == "translate"
        )
        detail = client.get(f"/commands/files/{summary['id']}", headers=AUTH_HEADERS)
        assert detail.status_code == HTTP_OK
        assert detail.json()["revision"]

        created = client.post(
            "/commands/files",
            headers=AUTH_HEADERS,
            json={"command": "reports/weekly", "format": "markdown"},
        )
        assert created.status_code == HTTP_OK
        created_payload = created.json()
        assert created_payload["command"] == "reports/weekly"
        assert (config_dir / "commands/reports/weekly.md").is_file()

        # Duplicate create is rejected without overwriting.
        duplicate = client.post(
            "/commands/files",
            headers=AUTH_HEADERS,
            json={"command": "reports/weekly", "format": "markdown"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "command_file_exists"

        updated = client.put(
            f"/commands/files/{created_payload['id']}",
            headers=AUTH_HEADERS,
            json={
                "content": "---\nname: Weekly\nbrain: none\n---\nUpdated body.\n",
                "expected_revision": created_payload["revision"],
            },
        )
        assert updated.status_code == HTTP_OK
        assert updated.json()["revision"] != created_payload["revision"]
        assert "Updated body." in (config_dir / "commands/reports/weekly.md").read_text(
            encoding="utf-8"
        )

        extra_update_field = client.put(
            f"/commands/files/{created_payload['id']}",
            headers=AUTH_HEADERS,
            json={
                "content": updated.json()["content"],
                "expected_revision": updated.json()["revision"],
                "unexpected": True,
            },
        )
        assert extra_update_field.status_code == HTTP_UNPROCESSABLE_ENTITY

        # Stale revision now conflicts.
        stale = client.put(
            f"/commands/files/{created_payload['id']}",
            headers=AUTH_HEADERS,
            json={
                "content": "---\nname: Weekly\n---\nAgain.\n",
                "expected_revision": created_payload["revision"],
            },
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "command_file_changed"

        missing = client.get("/commands/files/bm9wZQ", headers=AUTH_HEADERS)
        assert missing.status_code == 404

        # A stale revision refuses the delete instead of discarding the file.
        current_revision = updated.json()["revision"]
        stale_delete = client.delete(
            f"/commands/files/{created_payload['id']}"
            f"?expected_revision={created_payload['revision']}",
            headers=AUTH_HEADERS,
        )
        assert stale_delete.status_code == 409
        assert stale_delete.json()["code"] == "command_file_changed"
        assert (config_dir / "commands/reports/weekly.md").is_file()

        deleted = client.delete(
            f"/commands/files/{created_payload['id']}"
            f"?expected_revision={current_revision}",
            headers=AUTH_HEADERS,
        )
        assert deleted.status_code == HTTP_OK
        assert "reports/weekly" not in {
            item["command"] for item in deleted.json()["files"]
        }
        assert not (config_dir / "commands/reports/weekly.md").exists()

        # The removed file is gone for good.
        assert (
            client.delete(
                f"/commands/files/{created_payload['id']}"
                f"?expected_revision={current_revision}",
                headers=AUTH_HEADERS,
            ).status_code
            == 404
        )


def test_command_file_execution_status_and_run_guard(
    client: TestClient, workspace: Path
) -> None:
    config_dir = workspace / ".guildbotics/config"
    env_file = workspace / ".env"

    with client:
        _init_project(client, config_dir, env_file, language="en")
        client.post(
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
        created = client.post(
            "/commands/files",
            headers=AUTH_HEADERS,
            json={"command": "notes", "format": "markdown"},
        ).json()

        status = client.get(
            f"/commands/files/{created['id']}/execution-status",
            headers=AUTH_HEADERS,
            params={"person": "local-agent", "expected_revision": created["revision"]},
        )
        assert status.status_code == HTTP_OK
        assert status.json()["matches_selected_file"] is True

        # A stale revision blocks with the shared error code, matching the run
        # guard below.
        stale_status = client.get(
            f"/commands/files/{created['id']}/execution-status",
            headers=AUTH_HEADERS,
            params={"person": "local-agent", "expected_revision": "stale"},
        )
        assert stale_status.json()["blocking_code"] == "command_file_changed"

        run = client.post(
            "/commands/run",
            headers=AUTH_HEADERS,
            json={
                "command": "notes",
                "person": "local-agent",
                "expected_command_file_id": created["id"],
                "expected_command_file_revision": "stale",
            },
        )
        assert run.status_code == 409
        assert run.json()["code"] == "command_file_changed"

        invalid_source = (
            "---\nbrain: agent\nargs:\n  - name: text\n---\nPolish the input text.\n"
        )
        saved = client.put(
            f"/commands/files/{created['id']}",
            headers=AUTH_HEADERS,
            json={
                "content": invalid_source,
                "expected_revision": created["revision"],
            },
        )
        assert saved.status_code == HTTP_OK
        assert (config_dir / "commands/notes.md").read_text(
            encoding="utf-8"
        ) == invalid_source

        invalid_status = client.get(
            f"/commands/files/{created['id']}/execution-status",
            headers=AUTH_HEADERS,
            params={
                "person": "local-agent",
                "expected_revision": saved.json()["revision"],
            },
        )
        assert invalid_status.status_code == HTTP_OK
        assert invalid_status.json()["blocking_code"] == "command_file_invalid_source"

        options = client.get("/commands/options", headers=AUTH_HEADERS)
        assert options.status_code == HTTP_OK
        notes = next(
            option
            for option in options.json()["options"]
            if option["command"] == "notes"
        )
        assert notes["arguments"] == []

        invalid_run = client.post(
            "/commands/run",
            headers=AUTH_HEADERS,
            json={
                "command": "notes",
                "person": "local-agent",
                "expected_command_file_id": created["id"],
                "expected_command_file_revision": saved.json()["revision"],
            },
        )
        assert invalid_run.status_code == 409
        assert invalid_run.json()["code"] == "command_file_invalid_source"


def test_run_request_expected_pair_must_be_complete(
    client: TestClient, workspace: Path
) -> None:
    with client:
        response = client.post(
            "/commands/run",
            headers=AUTH_HEADERS,
            json={
                "command": "notes",
                "expected_command_file_id": "abc",
            },
        )
        assert response.status_code == 422


def test_command_files_endpoints_require_session_token(
    client: TestClient, workspace: Path
) -> None:
    with client:
        assert client.get("/commands/files").status_code == 401
        assert client.get("/commands/files/anything").status_code == 401
        assert (
            client.post(
                "/commands/files",
                json={"command": "x", "format": "markdown"},
            ).status_code
            == 401
        )
        assert (
            client.put(
                "/commands/files/anything",
                json={"content": "", "expected_revision": ""},
            ).status_code
            == 401
        )
        assert (
            client.delete("/commands/files/anything?expected_revision=x").status_code
            == 401
        )


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


def test_temp_workspace_intelligence_effort_round_trips_through_the_api(
    client: TestClient, workspace: Path
) -> None:
    """Effort survives GET -> PUT -> YAML with its provider-shaped types."""
    config_dir = workspace / ".guildbotics/config"
    env_file = workspace / ".env"

    with client:
        _init_project(client, config_dir, env_file)

        payload = client.get("/config/intelligences", headers=AUTH_HEADERS).json()
        model_path = payload["model_mapping"]["default"]
        for model in payload["models"]:
            if model["path"] == model_path:
                # Provider-shaped: the descriptor for this provider is what
                # decides which keys are acceptable here.
                model["effort"] = {
                    "high": {"reasoning_effort": "high"},
                    "low": {"reasoning_effort": "minimal"},
                }
        for agent in payload["cli_agents"]:
            agent["effort"] = {"high": {"effort": "high"}}

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

        reread = client.get("/config/intelligences", headers=AUTH_HEADERS).json()

    written = safe_load((config_dir / f"intelligences/{model_path}").read_text())
    assert written["effort"]["high"] == {"reasoning_effort": "high"}
    assert written["effort"]["low"] == {"reasoning_effort": "minimal"}

    reread_model = next(m for m in reread["models"] if m["path"] == model_path)
    assert reread_model["effort"]["low"] == {"reasoning_effort": "minimal"}
    # The descriptor travels with the definition so the editor can offer typed
    # controls without knowing anything about this provider.
    assert {field["key"] for field in reread_model["effort_fields"]} == {
        "reasoning_effort",
        "id",
    }
    assert all(
        agent["effort"] == {"high": {"effort": "high"}}
        for agent in reread["cli_agents"]
    )


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
