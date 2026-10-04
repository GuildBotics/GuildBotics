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
HTTP_BAD_REQUEST = 400
HTTP_CONFLICT = 409
HTTP_UNPROCESSABLE_ENTITY = 422

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}
PROJECT = "team/project.yml"
CLI_MAPPING = "intelligences/cli_agent_mapping.yml"


@pytest.mark.parametrize(
    "person_id",
    [
        "/private/keys",
        "../../keys",
        "a/b",
        "a\\b",
        "",
        "con",
        "aux",
        "nul",
        "com1",
        "lpt1",
    ],
)
def test_intelligence_api_rejects_invalid_member_before_any_read_write_or_delete(
    client, config_dir, person_id
):
    sentinel = config_dir / "intelligences/keep"
    sentinel.write_text("keep")
    before = {
        str(p.relative_to(config_dir)): p.read_bytes()
        for p in config_dir.rglob("*")
        if p.is_file()
    }
    response = client.get(
        "/config/intelligences", headers=AUTH_HEADERS, params={"person_id": person_id}
    )
    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    response = client.put(
        "/config/intelligences",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(config_dir),
            "person_id": person_id,
            "inherit_team_defaults": True,
        },
    )
    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert {
        str(p.relative_to(config_dir)): p.read_bytes()
        for p in config_dir.rglob("*")
        if p.is_file()
    } == before


@pytest.mark.parametrize(
    "stored_name",
    ["con", "aux", "nul", "alice", "Alice", "alice.bak", "alice backup", "あいこ"],
)
@pytest.mark.parametrize("action", ["rename", "delete"])
def test_invalid_stored_member_is_addressable_for_repair_but_never_execution(
    client, config_dir, stored_name, action
):
    import os

    from guildbotics.loader.yaml.yaml_team_loader import YamlTeamLoader
    from guildbotics.utils.person_id import MemberConfigError

    if os.name == "nt" and stored_name in {"con", "aux", "nul"}:
        pytest.skip("Windows cannot create a legacy reserved directory")
    member = config_dir / "team/members" / stored_name
    member.mkdir(parents=True)
    person = member / "person.yml"
    person.write_text(
        f"person_id: {'bob' if stored_name == 'alice' else stored_name}\nname: Legacy\nis_active: true\n"
    )
    with pytest.raises(MemberConfigError) as error:
        YamlTeamLoader(str(config_dir / "team")).load()
    assert error.value.filename == str(person)
    assert str(person) in str(error.value)
    listing = client.get("/team", headers=AUTH_HEADERS)
    assert stored_name in [p["person_id"] for p in listing.json()["members"]]
    assert str(person) in listing.json()["problem"]
    read = client.get(f"/config/members/{stored_name}", headers=AUTH_HEADERS)
    assert read.status_code == HTTP_OK
    assert read.json()["person_id"] == stored_name
    revisions = read.json()["revisions"]
    if action == "rename":
        repaired = "alice" if stored_name == "Alice" else "repaired"
        response = client.put(
            f"/config/members/{stored_name}",
            headers=AUTH_HEADERS,
            json=_member_payload(
                config_dir,
                revisions,
                original_person_id=stored_name,
                person_id=repaired,
            ),
        )
        assert response.status_code == HTTP_OK
        names = [child.name for child in member.parent.iterdir()]
        assert stored_name not in names
        assert repaired in names
        assert (
            safe_load(
                (config_dir / "team/members" / repaired / "person.yml").read_text()
            )["person_id"]
            == repaired
        )
    else:
        response = client.request(
            "DELETE",
            f"/config/members/{stored_name}",
            headers=AUTH_HEADERS,
            json={"config_dir": str(config_dir), "expected_revisions": revisions},
        )
        assert response.status_code == HTTP_OK
        assert not member.exists()
    assert client.get("/team", headers=AUTH_HEADERS).json().get("problem", "") == ""


@pytest.mark.parametrize("action", ["rename", "delete"])
@pytest.mark.parametrize(
    "content",
    [
        "",
        "[alice]",
        "person_id: [",
        "person_id: alice\naccount_info: null\nprofile: null\nroutine_commands: null\n",
        "person_id: alice\naccount_info: []\nprofile: []\nroutine_commands: 123\n",
        "person_id: alice\nprofile:\n  roles: {123: architect}\n  character: {123: invalid}\n",
    ],
)
def test_invalid_member_data_remains_listed_and_repairable(
    client, config_dir, content, action
):
    member = config_dir / "team/members/broken"
    member.mkdir(parents=True)
    person = member / "person.yml"
    person.write_text(content)
    listing = client.get("/team", headers=AUTH_HEADERS)
    assert listing.status_code == HTTP_OK
    assert "broken" in [p["person_id"] for p in listing.json()["members"]]
    assert str(person) in listing.json()["problem"]
    read = client.get("/config/members/broken", headers=AUTH_HEADERS)
    assert read.status_code == HTTP_OK
    if action == "rename":
        response = client.put(
            "/config/members/broken",
            headers=AUTH_HEADERS,
            json=_member_payload(
                config_dir,
                read.json()["revisions"],
                original_person_id="broken",
                person_id="repaired",
            ),
        )
    else:
        response = client.request(
            "DELETE",
            "/config/members/broken",
            headers=AUTH_HEADERS,
            json={
                "config_dir": str(config_dir),
                "expected_revisions": read.json()["revisions"],
            },
        )
    assert response.status_code == HTTP_OK
    assert client.get("/team", headers=AUTH_HEADERS).json().get("problem", "") == ""


def test_directories_without_member_config_are_not_runtime_members(client, config_dir):
    (config_dir / "team/members/alice.bak").mkdir(parents=True)
    listing = client.get("/team", headers=AUTH_HEADERS)
    assert listing.status_code == HTTP_OK
    assert listing.json().get("problem", "") == ""
    assert listing.json()["members"] == []


@pytest.mark.parametrize("name", ["Alice", "alice.bak", "alice backup", "あいこ"])
def test_repair_read_requires_an_actual_stored_member(client, config_dir, name):
    directory = config_dir / "team/members" / name
    directory.mkdir(parents=True)
    response = client.get(f"/config/members/{name}", headers=AUTH_HEADERS)
    assert response.status_code == HTTP_BAD_REQUEST
    assert not list(directory.iterdir())


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
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

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == "config_revision_invalid"
