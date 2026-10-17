"""Config saves refused when the screen was composed against older content.

These run against a real temporary workspace, because what is being checked is
whether the bytes on disk survive a save made from a stale screen.
"""

from __future__ import annotations

from pathlib import Path

import i18n
import pytest
from fastapi.testclient import TestClient
from yaml import safe_load

from guildbotics.utils.i18n_tool import t
from guildbotics.utils.person_id import IDENTITY_FILE, MEMBER_FILE, identity_members_dir
from tests.guildbotics.members import write_member

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
    ["con", "aux", "nul", "Alice", "alice.bak", "alice backup", "あいこ"],
)
def test_a_member_stored_under_no_valid_id_is_listed_and_can_be_removed(
    client, config_dir, stored_name
):
    """No identity can have its name, so the workspace's file of it is all
    there is: listed with the problem, and removed from the workspace."""
    import os

    from guildbotics.loader.yaml.yaml_team_loader import YamlTeamLoader

    if os.name == "nt" and stored_name in {"con", "aux", "nul"}:
        pytest.skip("Windows cannot create a legacy reserved directory")
    member = config_dir / "team/members" / stored_name
    member.mkdir(parents=True)
    (member / MEMBER_FILE).write_text("is_active: true\n", encoding="utf-8")
    identity = identity_members_dir() / stored_name / IDENTITY_FILE
    # The rest of the team loads without it.
    assert YamlTeamLoader(str(config_dir / "team")).load().members == []
    listing = client.get("/team", headers=AUTH_HEADERS)
    assert stored_name in [p["person_id"] for p in listing.json()["members"]]
    assert str(identity) in listing.json()["problem"]
    read = client.get(f"/config/members/{stored_name}", headers=AUTH_HEADERS)
    assert read.status_code == HTTP_OK
    assert read.json()["person_id"] == stored_name

    response = client.delete(f"/config/members/{stored_name}", headers=AUTH_HEADERS)

    assert response.status_code == HTTP_OK
    assert not member.exists()
    assert client.get("/team", headers=AUTH_HEADERS).json().get("problem", "") == ""


def test_an_identity_naming_another_member_is_repaired_by_saving_it(client, config_dir):
    from guildbotics.loader.yaml.yaml_team_loader import YamlTeamLoader
    from guildbotics.utils.person_id import MemberConfigError

    write_member(config_dir, {"person_id": "alice", "name": "Legacy"})
    identity = identity_members_dir() / "alice" / IDENTITY_FILE
    identity.write_text("person_id: bob\nname: Legacy\n", encoding="utf-8")
    with pytest.raises(MemberConfigError) as error:
        YamlTeamLoader(str(config_dir / "team")).load()
    assert error.value.filename == str(identity)
    read = client.get("/config/members/alice", headers=AUTH_HEADERS)

    response = client.put(
        "/config/members/alice",
        headers=AUTH_HEADERS,
        json=_member_payload(config_dir, read.json()["revisions"]),
    )

    assert response.status_code == HTTP_OK
    assert safe_load(identity.read_text(encoding="utf-8"))["person_id"] == "alice"
    assert client.get("/team", headers=AUTH_HEADERS).json().get("problem", "") == ""


@pytest.mark.parametrize("action", ["save", "remove"])
@pytest.mark.parametrize(
    ("broken", "content"),
    [
        ("member", ""),
        ("member", "[broken]"),
        ("member", "is_active: ["),
        ("member", "routine_commands: 123\nprofile: []\n"),
        ("member", "name: Misplaced\n"),
        ("identity", ""),
        ("identity", "person_id: ["),
        ("identity", "person_id: broken\naccount_info: []\nprofile: []\n"),
        ("identity", "person_id: broken\nis_active: true\n"),
    ],
)
def test_invalid_member_data_remains_listed_and_repairable(
    client, config_dir, broken, content, action
):
    member = write_member(config_dir, {"person_id": "broken", "name": "Broken"})
    path = (
        member / MEMBER_FILE
        if broken == "member"
        else identity_members_dir() / "broken" / IDENTITY_FILE
    )
    path.write_text(content, encoding="utf-8")
    listing = client.get("/team", headers=AUTH_HEADERS)
    assert listing.status_code == HTTP_OK
    assert "broken" in [p["person_id"] for p in listing.json()["members"]]
    assert str(path) in listing.json()["problem"]
    read = client.get("/config/members/broken", headers=AUTH_HEADERS)
    assert read.status_code == HTTP_OK
    if action == "save":
        response = client.put(
            "/config/members/broken",
            headers=AUTH_HEADERS,
            json=_member_payload(
                config_dir, read.json()["revisions"], person_id="broken"
            ),
        )
    else:
        response = client.delete("/config/members/broken", headers=AUTH_HEADERS)
    assert response.status_code == HTTP_OK
    assert client.get("/team", headers=AUTH_HEADERS).json().get("problem", "") == ""


def test_directories_without_member_config_are_not_runtime_members(client, config_dir):
    (config_dir / "team/members/alice.bak").mkdir(parents=True)
    listing = client.get("/team", headers=AUTH_HEADERS)
    assert listing.status_code == HTTP_OK
    assert listing.json().get("problem", "") == ""
    assert listing.json()["members"] == []


def test_colliding_members_are_repairable_without_changing_shared_secrets(
    client, config_dir, monkeypatch
):
    """Two identities whose secrets share a name, though only one of them is in
    this workspace: deleting one leaves the shared value alone."""
    from guildbotics.utils.secret_store import KeyringSecretStore, identity_secret_store

    write_member(config_dir, {"person_id": "alice-bak", "name": "Test"})
    other = identity_members_dir() / "alice_bak"
    other.mkdir(parents=True)
    (other / IDENTITY_FILE).write_text("person_id: alice_bak\nname: Test\n")
    store = identity_secret_store()
    store.set("ALICE_BAK_SLACK_BOT_TOKEN", "fake-shared-token")
    before = store.location.read_bytes()
    listing = client.get("/team", headers=AUTH_HEADERS)
    assert listing.status_code == HTTP_OK
    assert str(other / IDENTITY_FILE) in listing.json()["problem"]

    def unexpected(*_args, **_kwargs):
        pytest.fail("repair accessed ambiguous secrets")

    with monkeypatch.context() as guarded:
        for operation in ("get", "set", "rename", "delete", "keys"):
            guarded.setattr(KeyringSecretStore, operation, unexpected)
        read = client.get("/config/members/alice-bak", headers=AUTH_HEADERS)
        assert read.status_code == HTTP_OK
        assert read.json()["accounts"]["slack"]["has_bot_token"] is False
        assert read.json()["secret_env_keys"] == {}
        response = client.delete("/identities/alice_bak", headers=AUTH_HEADERS)
        assert response.status_code == HTTP_OK
    assert store.location.read_bytes() == before
    assert store.get("ALICE_BAK_SLACK_BOT_TOKEN") == "fake-shared-token"
    assert client.get("/team", headers=AUTH_HEADERS).json()["problem"] == ""


@pytest.mark.parametrize("language", ["en", "ja"])
def test_candidate_namespace_conflict_offers_a_different_id(
    client, config_dir, language
):
    """An identity of another workspace counts: the store is the same."""
    for name in ("alice-bak", "bob"):
        directory = identity_members_dir() / name
        directory.mkdir(parents=True)
        (directory / IDENTITY_FILE).write_text(f"person_id: {name}\nname: Test\n")
    files = [identity_members_dir(), config_dir / "team/members"]

    def stored() -> dict[Path, bytes]:
        return {
            path: path.read_bytes()
            for root in files
            for path in root.rglob("*")
            if path.is_file()
        }

    before = stored()
    payload = _member_payload(config_dir, {}, person_id="alice_bak")
    previous = i18n.get("locale")
    i18n.set("locale", language)
    try:
        response = client.post("/config/members", headers=AUTH_HEADERS, json=payload)
        assert response.status_code == HTTP_BAD_REQUEST
        assert response.json()["code"] == "person_env_prefix_conflict"
        assert response.json()["message"] == t(
            "member_config.target_prefix_conflict",
            person_id="alice_bak",
            members="alice-bak",
        )
    finally:
        i18n.set("locale", previous)
    assert stored() == before


@pytest.mark.parametrize("name", ["Alice", "alice.bak", "alice backup", "あいこ"])
def test_repair_read_requires_an_actual_stored_member(client, config_dir, name):
    directory = config_dir / "team/members" / name
    directory.mkdir(parents=True)
    response = client.get(f"/config/members/{name}", headers=AUTH_HEADERS)
    assert response.status_code == HTTP_BAD_REQUEST
    assert not list(directory.iterdir())


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
    }
    payload.update(overrides)
    return payload


def _member_payload(config_dir: Path, revisions: dict[str, str], **overrides) -> dict:
    payload = {
        "config_dir": str(config_dir),
        "expected_revisions": revisions,
        "person_type": "",
        "person_id": "alice",
        "person_name": "Alice",
        "is_active": True,
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
            "git_email": "",
            "roles": ["architect"],
            "speaking_style": "concise",
        },
    )
    assert created.status_code == HTTP_OK
    identity = "team/members/alice/identity.yml"
    member = "team/members/alice/member.yml"

    stale = client.get("/config/members/alice", headers=AUTH_HEADERS).json()[
        "revisions"
    ]
    assert set(stale) == {identity, member}
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
    stored = safe_load(
        (identity_members_dir() / "alice" / IDENTITY_FILE).read_text(encoding="utf-8")
    )
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


def test_a_member_save_reports_both_files_it_wrote(
    client: TestClient, config_dir: Path
) -> None:
    """The identity and the workspace's file of the member are written as one
    save, so the revisions a following save needs are of both."""
    client.post(
        "/config/members",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(config_dir),
            "person_type": "",
            "person_id": "alice",
            "person_name": "Alice",
            "is_active": True,
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
        json=_member_payload(config_dir, revisions, person_name="Saved once"),
    ).json()

    assert set(written["revisions"]) == {
        "team/members/alice/identity.yml",
        "team/members/alice/member.yml",
    }
    again = client.put(
        "/config/members/alice",
        headers=AUTH_HEADERS,
        json=_member_payload(
            config_dir, written["revisions"], person_name="Saved twice"
        ),
    )
    assert again.status_code == HTTP_OK


def test_a_member_keeps_its_id(client: TestClient, config_dir: Path) -> None:
    """Other workspaces, perhaps on other machines, name the member by its ID,
    so a save under another ID is refused rather than taken for a rename."""
    write_member(config_dir, {"person_id": "alice", "name": "Alice"})
    revisions = client.get("/config/members/alice", headers=AUTH_HEADERS).json()[
        "revisions"
    ]

    response = client.put(
        "/config/members/alice",
        headers=AUTH_HEADERS,
        json=_member_payload(config_dir, revisions, person_id="alice2"),
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == "person_id_mismatch"
    assert not (identity_members_dir() / "alice2").exists()


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


# -- a member's revisions describe what was read and written -----------------


def _member_files(config_dir: Path) -> dict[str, Path]:
    return {
        "identity": identity_members_dir() / "alice" / IDENTITY_FILE,
        "member": config_dir / "team/members/alice" / MEMBER_FILE,
    }


def _change_elsewhere(path: Path) -> None:
    """Another writer -- a save elsewhere, or synchronization -- changing the
    file through the shared-write port, as every writer does."""
    from guildbotics.utils.workspace_sync_port import update_shared_text

    update_shared_text(path, lambda text: (text or "") + "# changed elsewhere\n")


@pytest.mark.parametrize("changed", ["identity", "member"])
def test_a_member_read_never_hands_out_a_revision_newer_than_its_content(
    client: TestClient,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    """A form whose revision is newer than what it shows would save over the
    change it never showed. Whatever lands while the member is read, saving
    the form is refused."""
    from guildbotics.setup.setup_service import SimplePersonSetupService

    write_member(config_dir, {"person_id": "alice", "name": "Alice", "is_active": True})
    read = SimplePersonSetupService.read_person_config

    def read_then_changed(self, **kwargs):
        snapshot = read(self, **kwargs)
        _change_elsewhere(_member_files(config_dir)[changed])
        return snapshot

    with monkeypatch.context() as patched:
        patched.setattr(
            SimplePersonSetupService, "read_person_config", read_then_changed
        )
        form = client.get("/config/members/alice", headers=AUTH_HEADERS).json()

    response = client.put(
        "/config/members/alice",
        headers=AUTH_HEADERS,
        json=_member_payload(config_dir, form["revisions"]),
    )

    assert response.status_code == HTTP_CONFLICT, response.text
    assert "# changed elsewhere" in _member_files(config_dir)[changed].read_text()


@pytest.mark.parametrize("changed", ["identity", "member"])
def test_a_member_save_answers_with_the_revisions_it_wrote(
    client: TestClient,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    """Each file's revision is read before its lock is released: a writer that
    gets in right after is a change the open form has not seen, and its next
    save is refused instead of written over it."""
    import guildbotics.app_api.api as api_module
    from guildbotics.utils.fileio import get_identity_config_dir

    write_member(config_dir, {"person_id": "alice", "name": "Alice", "is_active": True})
    revisions = client.get("/config/members/alice", headers=AUTH_HEADERS).json()[
        "revisions"
    ]
    write = api_module.apply_config_write
    locked = get_identity_config_dir() if changed == "identity" else config_dir

    def then_changed(target: Path, *args, **kwargs):
        receipt = write(target, *args, **kwargs)
        if target == locked:
            _change_elsewhere(_member_files(config_dir)[changed])
        return receipt

    with monkeypatch.context() as patched:
        patched.setattr(api_module, "apply_config_write", then_changed)
        saved = client.put(
            "/config/members/alice",
            headers=AUTH_HEADERS,
            json=_member_payload(config_dir, revisions, person_name="Saved"),
        )
    assert saved.status_code == HTTP_OK

    again = client.put(
        "/config/members/alice",
        headers=AUTH_HEADERS,
        json=_member_payload(
            config_dir, saved.json()["revisions"], person_name="Again"
        ),
    )

    assert again.status_code == HTTP_CONFLICT, again.text
    assert "# changed elsewhere" in _member_files(config_dir)[changed].read_text()


def test_a_member_deletion_answers_with_what_it_left(
    client: TestClient, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace's file is gone when its lock is released; a member of the
    same ID made right after is not described as the one deleted."""
    import guildbotics.app_api.api as api_module

    write_member(config_dir, {"person_id": "alice", "name": "Alice"})
    write = api_module.apply_config_write

    def then_recreated(target: Path, *args, **kwargs):
        receipt = write(target, *args, **kwargs)
        if target == config_dir:
            write_member(config_dir, {"person_id": "alice", "name": "Alice again"})
        return receipt

    with monkeypatch.context() as patched:
        patched.setattr(api_module, "apply_config_write", then_recreated)
        deleted = client.delete("/identities/alice", headers=AUTH_HEADERS)

    assert deleted.status_code == HTTP_OK
    assert deleted.json()["revisions"]["team/members/alice/member.yml"] == ""
