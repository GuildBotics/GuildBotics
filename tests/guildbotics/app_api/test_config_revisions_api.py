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


def _intelligence_payload(
    config_dir: Path, read: dict, revisions: dict[str, str], **overrides
) -> dict:
    payload = {
        "config_dir": str(config_dir),
        "expected_revisions": revisions,
        "model_mapping": read["model_mapping"],
        "models": read["models"],
        "cli_agent_mapping": read["cli_agent_mapping"],
        "cli_agents": read["cli_agents"],
        "brain_mapping": read["brain_mapping"],
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
        json=_intelligence_payload(config_dir, read, read["revisions"]),
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "config_changed"
    assert (config_dir / "intelligences/model_mapping.yml").read_text(
        encoding="utf-8"
    ) == "default: gpt-4o\n"


def test_an_intelligence_save_is_refused_when_a_file_was_added_to_the_directory(
    client: TestClient, config_dir: Path
) -> None:
    """The screen prunes what it did not read, so a file it never saw is not
    something it may quietly delete."""
    read = client.get("/config/intelligences", headers=AUTH_HEADERS).json()
    added = config_dir / "intelligences/models/openai/o9.yml"
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_text("model_class: x\n", encoding="utf-8")

    response = client.put(
        "/config/intelligences",
        headers=AUTH_HEADERS,
        json=_intelligence_payload(config_dir, read, read["revisions"]),
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "config_changed"
    assert added.exists()


def test_a_member_inheriting_the_team_defaults_is_still_guarded(
    client: TestClient, config_dir: Path
) -> None:
    """A member with no override directory has nothing to name file by file, and
    an empty expectation would apply the save without any comparison at all."""
    client.post(
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
    read = client.get(
        "/config/intelligences?person_id=alice", headers=AUTH_HEADERS
    ).json()
    assert read["revisions"]
    override = config_dir / "team/members/alice/intelligences"
    override.mkdir(parents=True)
    (override / "model_mapping.yml").write_text("{}\n", encoding="utf-8")

    response = client.put(
        "/config/intelligences",
        headers=AUTH_HEADERS,
        json=_intelligence_payload(
            config_dir,
            read,
            read["revisions"],
            person_id="alice",
            inherit_team_defaults=True,
        ),
    )

    assert response.status_code == HTTP_CONFLICT
    assert response.json()["code"] == "config_changed"
    assert (override / "model_mapping.yml").exists()


def test_an_intelligence_save_reports_where_its_directory_now_stands(
    client: TestClient, config_dir: Path
) -> None:
    """The advanced editor stays open, so its second save needs this rather than
    the revisions the screen was loaded with."""
    read = client.get("/config/intelligences", headers=AUTH_HEADERS).json()

    written = client.put(
        "/config/intelligences",
        headers=AUTH_HEADERS,
        json=_intelligence_payload(config_dir, read, read["revisions"]),
    ).json()

    assert (
        written["revisions"]
        == (
            client.get("/config/intelligences", headers=AUTH_HEADERS).json()[
                "revisions"
            ]
        )
    )
    again = client.put(
        "/config/intelligences",
        headers=AUTH_HEADERS,
        json=_intelligence_payload(config_dir, read, written["revisions"]),
    )
    assert again.status_code == HTTP_OK


def test_a_save_reports_where_the_files_it_wrote_now_stand(
    client: TestClient, config_dir: Path
) -> None:
    """The screen stays open, so its next save needs this rather than the
    revisions it was loaded with."""
    revisions = client.get("/config/project", headers=AUTH_HEADERS).json()["revisions"]

    written = client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json=_project_payload(config_dir, revisions, description="Renamed"),
    ).json()

    assert (
        written["revisions"]
        == (client.get("/config/project", headers=AUTH_HEADERS).json()["revisions"])
    )
    assert written["revisions"][PROJECT] != revisions[PROJECT]


def test_saving_twice_from_the_same_screen_is_not_a_conflict(
    client: TestClient, config_dir: Path
) -> None:
    """A screen colliding with its own previous save would report a conflict
    with another machine where there is none, and discard the second edit."""
    revisions = client.get("/config/project", headers=AUTH_HEADERS).json()["revisions"]

    first = client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json=_project_payload(config_dir, revisions, description="First"),
    )
    second = client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json=_project_payload(
            config_dir, first.json()["revisions"], description="Second"
        ),
    )

    assert second.status_code == HTTP_OK
    stored = safe_load((config_dir / PROJECT).read_text(encoding="utf-8"))
    assert stored["description"] == "Second"


def test_a_member_save_reports_the_paths_it_ended_up_writing(
    client: TestClient, config_dir: Path
) -> None:
    """A rename moves the file, so the revisions that matter afterwards are the
    new path's, not the ones the request was composed against."""
    client.post(
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
    revisions = client.get("/config/members/alice", headers=AUTH_HEADERS).json()[
        "revisions"
    ]

    written = client.put(
        "/config/members/alice",
        headers=AUTH_HEADERS,
        json=_member_payload(config_dir, revisions, person_id="alice2"),
    ).json()

    assert set(written["revisions"]) == {"team/members/alice2/person.yml"}
    # And that revision is the one a following save must use.
    again = client.put(
        "/config/members/alice2",
        headers=AUTH_HEADERS,
        json=_member_payload(
            config_dir,
            written["revisions"],
            person_id="alice2",
            original_person_id="alice2",
            person_name="Renamed twice",
        ),
    )
    assert again.status_code == HTTP_OK


def test_a_file_created_after_the_read_is_not_overwritten_unseen(
    client: TestClient, config_dir: Path
) -> None:
    """An absent file is a revision of its own: something that appears between
    the read and the save is a change like any other."""
    (config_dir / CLI_MAPPING).unlink()
    revisions = client.get("/config/project", headers=AUTH_HEADERS).json()["revisions"]
    assert revisions[CLI_MAPPING] == ""
    (config_dir / CLI_MAPPING).write_text("default: claude\n", encoding="utf-8")

    response = client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json=_project_payload(
            config_dir, revisions, description="From the stale screen"
        ),
    )

    assert response.status_code == HTTP_CONFLICT
    assert (config_dir / CLI_MAPPING).read_text(encoding="utf-8") == "default: claude\n"


def test_a_revision_naming_something_outside_config_is_a_bad_request(
    client: TestClient, config_dir: Path
) -> None:
    response = client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json=_project_payload(config_dir, {"../state/workspace.json": "abc"}),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "config_revision_invalid"
