"""Config saves refused when the screen was composed against older content.

These run against a real temporary workspace, because what is being checked is
whether the bytes on disk survive a save made from a stale screen.
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
HTTP_CONFLICT = 409

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}
PROJECT = "team/project.yml"
CLI_MAPPING = "intelligences/cli_agent_mapping.yml"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def client(workspace: Path) -> TestClient:
    return TestClient(
        create_app(session_token="secret", runtime=AppRuntime(EventBus()))
    )


@pytest.fixture
def config_dir(client: TestClient, workspace: Path) -> Path:
    path = workspace / ".guildbotics/config"
    response = client.post(
        "/config/init",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(path),
            "language": "en",
            "description": "Temp automation workspace",
            "llm_api_type": "openai",
            "cli_agent": "codex",
            "provider_api_keys": {"openai": "test-openai-key"},
        },
    )
    assert response.status_code == HTTP_OK
    return path


def _project_payload(config_dir: Path, revisions: dict[str, str], **overrides) -> dict:
    payload = {
        "config_dir": str(config_dir),
        "expected_revisions": revisions,
        "language": "en",
        "description": "Temp automation workspace",
        "llm_api_type": "openai",
        "cli_agent": "codex",
        "github_enabled": False,
    }
    payload.update(overrides)
    return payload


def _member_payload(config_dir: Path, revisions: dict[str, str], **overrides) -> dict:
    payload = {
        "config_dir": str(config_dir),
        "expected_revisions": revisions,
        "original_person_id": "alice",
        "person_type": "",
        "person_id": "alice",
        "person_name": "Alice",
        "is_active": True,
        "github_username": "",
        "git_email": "",
        "roles": ["architect"],
        "speaking_style": "concise",
    }
    payload.update(overrides)
    return payload


def test_a_project_read_reports_the_revision_of_every_file_it_used(
    client: TestClient, config_dir: Path
) -> None:
    payload = client.get("/config/project", headers=AUTH_HEADERS).json()

    assert set(payload["revisions"]) == {
        PROJECT,
        "intelligences/model_mapping.yml",
        CLI_MAPPING,
    }


def test_a_project_save_at_the_reported_revisions_applies(
    client: TestClient, config_dir: Path
) -> None:
    revisions = client.get("/config/project", headers=AUTH_HEADERS).json()["revisions"]

    response = client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json=_project_payload(config_dir, revisions, description="Renamed"),
    )

    assert response.status_code == HTTP_OK
    stored = safe_load((config_dir / PROJECT).read_text(encoding="utf-8"))
    assert stored["description"] == "Renamed"


def test_a_project_save_from_a_stale_screen_keeps_the_newer_content(
    client: TestClient, config_dir: Path
) -> None:
    """The case synchronization makes ordinary: another machine got there first."""
    stale = client.get("/config/project", headers=AUTH_HEADERS).json()["revisions"]
    arrived = client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json=_project_payload(
            config_dir,
            client.get("/config/project", headers=AUTH_HEADERS).json()["revisions"],
            description="From the other machine",
        ),
    )
    assert arrived.status_code == HTTP_OK

    response = client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json=_project_payload(config_dir, stale, description="From the stale screen"),
    )

    assert response.status_code == HTTP_CONFLICT
    payload = response.json()
    assert payload["code"] == "config_changed"
    assert payload["context"]["path"] == f"config/{PROJECT}"
    stored = safe_load((config_dir / PROJECT).read_text(encoding="utf-8"))
    assert stored["description"] == "From the other machine"


def test_a_refusal_reports_the_revisions_to_reload_with(
    client: TestClient, config_dir: Path
) -> None:
    stale = client.get("/config/project", headers=AUTH_HEADERS).json()["revisions"]
    client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json=_project_payload(
            config_dir,
            client.get("/config/project", headers=AUTH_HEADERS).json()["revisions"],
            description="From the other machine",
        ),
    )

    refused = client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json=_project_payload(config_dir, stale, description="From the stale screen"),
    ).json()

    current = client.get("/config/project", headers=AUTH_HEADERS).json()["revisions"]
    assert refused["context"]["revisions"] == current


def test_a_stale_file_the_user_never_edited_still_refuses_the_save(
    client: TestClient, config_dir: Path
) -> None:
    """The project screen saves three files, so any of them can be superseded."""
    stale = client.get("/config/project", headers=AUTH_HEADERS).json()["revisions"]
    (config_dir / CLI_MAPPING).write_text("default: claude\n", encoding="utf-8")

    response = client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json=_project_payload(config_dir, stale, description="From the stale screen"),
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["context"]["path"] == f"config/{CLI_MAPPING}"
    stored = safe_load((config_dir / PROJECT).read_text(encoding="utf-8"))
    assert stored["description"] == "Temp automation workspace"


def test_first_time_setup_saves_without_revisions(
    client: TestClient, config_dir: Path
) -> None:
    """Sending no revisions applies the save: there is nothing to be stale against."""
    response = client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json=_project_payload(config_dir, {}, description="Renamed"),
    )

    assert response.status_code == HTTP_OK
    stored = safe_load((config_dir / PROJECT).read_text(encoding="utf-8"))
    assert stored["description"] == "Renamed"


def test_a_member_read_reports_its_revision_and_a_stale_save_is_refused(
    client: TestClient, config_dir: Path
) -> None:
    created = client.post(
        "/config/members",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(config_dir),
            "person_type": "",
            "person_id": "alice",
            "person_name": "Alice",
            "is_active": True,
            "github_username": "",
            "git_email": "",
            "roles": ["architect"],
            "speaking_style": "concise",
        },
    )
    assert created.status_code == HTTP_OK
    person = "team/members/alice/person.yml"

    stale = client.get("/config/members/alice", headers=AUTH_HEADERS).json()[
        "revisions"
    ]
    assert set(stale) == {person}
    applied = client.put(
        "/config/members/alice",
        headers=AUTH_HEADERS,
        json=_member_payload(config_dir, stale, person_name="From the other machine"),
    )
    assert applied.status_code == HTTP_OK

    response = client.put(
        "/config/members/alice",
        headers=AUTH_HEADERS,
        json=_member_payload(config_dir, stale, person_name="From the stale screen"),
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "config_changed"
    stored = safe_load((config_dir / person).read_text(encoding="utf-8"))
    assert stored["name"] == "From the other machine"


def test_an_intelligence_read_covers_the_whole_directory_it_reconciles(
    client: TestClient, config_dir: Path
) -> None:
    revisions = client.get("/config/intelligences", headers=AUTH_HEADERS).json()[
        "revisions"
    ]

    assert revisions
    assert all(path.startswith("intelligences/") for path in revisions)
    assert "intelligences/model_mapping.yml" in revisions


def test_an_intelligence_save_is_refused_when_the_directory_moved(
    client: TestClient, config_dir: Path
) -> None:
    read = client.get("/config/intelligences", headers=AUTH_HEADERS).json()
    (config_dir / "intelligences/model_mapping.yml").write_text(
        "default: gpt-4o\n", encoding="utf-8"
    )

    response = client.put(
        "/config/intelligences",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(config_dir),
            "expected_revisions": read["revisions"],
            "model_mapping": read["model_mapping"],
            "models": read["models"],
            "cli_agent_mapping": read["cli_agent_mapping"],
            "cli_agents": read["cli_agents"],
            "brain_mapping": read["brain_mapping"],
        },
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "config_changed"
    assert (config_dir / "intelligences/model_mapping.yml").read_text(
        encoding="utf-8"
    ) == "default: gpt-4o\n"
