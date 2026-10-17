"""Config-write coverage for the simple setup service.

These tests extend ``test_setup_service.py`` and focus on the file/YAML
write behaviours that the existing suite did not yet exercise:

- project init file set across workspace / home / custom config locations
- secret blank-update must not clear an existing secret
- member add / update / delete secret-key add / update / remove
- ``task_schedules`` / ``routine_commands`` YAML round trip (participation)
- character / relationships / speaking_style round trip (identity)

All filesystem writes use ``tmp_path`` so the real home directory is never
touched.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.setup.setup_service import (
    PersonSetupInput,
    ProjectSetupInput,
    SimplePersonSetupService,
    SimpleProjectSetupService,
)
from guildbotics.utils.fileio import (
    get_template_path,
    load_markdown_with_frontmatter,
    load_yaml_file,
)
from guildbotics.utils.person_id import IDENTITY_FILE, MEMBER_FILE, identity_members_dir
from guildbotics.utils.secret_store import KeyringSecretStore, identity_secret_store
from guildbotics.utils.text_utils import replace_placeholders

# Relative config-dir layout produced by ``write_project`` independent of the
# absolute config location (workspace / home / custom).
PROJECT_RELATIVE_FILES = {
    "team/project.yml",
    "intelligences/model_mapping.yml",
    "intelligences/cli_agent_mapping.yml",
    "commands/translate.md",
    "commands/summarize.md",
    "commands/context-info.md",
    "commands/get-time-of-day.yml",
}


def _project_input(config_dir: Path, **overrides) -> ProjectSetupInput:
    payload: dict = {
        "config_dir": config_dir,
        "language": "en",
        "llm_api_type": "openai",
        "cli_agent": "codex",
        "provider_api_keys": {"openai": "test-openai-key"},
    }
    payload.update(overrides)
    return ProjectSetupInput(**payload)


def _github_project_input(config_dir: Path, **overrides) -> ProjectSetupInput:
    return _project_input(
        config_dir,
        services={
            "code_hosting_service": {"name": "github", "owner": "GuildBotics"},
            "ticket_service": {
                "name": "github",
                "url": "https://github.com/orgs/GuildBotics/projects/1",
            },
        },
        **overrides,
    )


def _person_input(config_dir: Path, **overrides) -> PersonSetupInput:
    payload: dict = {
        "config_dir": config_dir,
        "person_type": "machine_user",
        "person_id": "alice",
        "person_name": "Alice",
        "is_active": True,
        "git_email": "1+alice@users.noreply.github.com",
        "roles": ["architect"],
        "speaking_style": "style-a",
    }
    payload.update(overrides)
    return PersonSetupInput(**payload)


def _tokens(github: str = "", slack_bot: str = "") -> dict:
    return {
        "github": {
            "account_type": "machine_user",
            "username": "alice",
            "access_token": github,
        },
        "slack": {"bot_token": slack_bot},
    }


def _secrets() -> dict[str, str]:
    return identity_secret_store().values()


def _identity(person_id: str = "alice") -> dict:
    return load_yaml_file(identity_members_dir() / person_id / IDENTITY_FILE)


def _participation(config_dir: Path, person_id: str = "alice") -> dict:
    return load_yaml_file(config_dir / "team/members" / person_id / MEMBER_FILE)


# --------------------------------------------------------------------------- #
# project init: file set per config location (workspace / sibling / custom)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "location",
    [".guildbotics/config", "sibling_config", "custom/place/config"],
    ids=["workspace", "sibling", "custom"],
)
def test_write_project_file_set_is_location_independent(
    tmp_path: Path, location: str
) -> None:
    config_dir = tmp_path / location
    result = SimpleProjectSetupService().write_project(
        _github_project_input(config_dir)
    )

    created_paths = {created_file.path for created_file in result.files}
    for relative in PROJECT_RELATIVE_FILES:
        expected = config_dir / relative
        assert expected in created_paths
        assert expected.exists()
    # The cli_agent config bundle is copied alongside the mappings.
    cli_agent_files = {
        path for path in created_paths if "intelligences/cli_agents/" in path.as_posix()
    }
    assert cli_agent_files
    for path in cli_agent_files:
        assert path.exists()
    assert KeyringSecretStore(config_dir).get("OPENAI_API_KEY") == "test-openai-key"


@pytest.mark.parametrize("language", ["en", "ja"])
def test_sample_command_brains_exist_in_template_mapping(language: str) -> None:
    sample_dir = Path("guildbotics/setup/templates/sample_commands") / language
    brain_mapping = load_yaml_file(
        get_template_path() / "intelligences/brain_mapping.yml"
    )

    referenced = {
        str(load_markdown_with_frontmatter(path).get("brain", "default"))
        for path in sample_dir.glob("*.md")
    }
    enabled = {brain for brain in referenced if brain not in {"none", "-", "null"}}

    assert enabled <= set(brain_mapping)


@pytest.mark.parametrize(
    ("template_language", "language_code", "language_name", "expected"),
    [
        ("ja", "ja", "日本語", "`input`フィールドのテキストが日本語であれば英語に"),
        ("ja", "en", "English", "`input`フィールドのテキストが日本語であれば英語に"),
        ("ja", "fr", "français", "`input`フィールドのテキストがfrançaisであれば英語に"),
        ("en", "ja", "日本語", "text in the `input` field is in 日本語"),
        ("en", "en", "English", "text in the `input` field is in Japanese"),
        ("en", "fr", "français", "text in the `input` field is in français"),
    ],
)
def test_translate_sample_uses_os_ui_language_without_arguments(
    template_language: str,
    language_code: str,
    language_name: str,
    expected: str,
) -> None:
    path = (
        Path("guildbotics/setup/templates/sample_commands")
        / template_language
        / "translate.md"
    )
    command = load_markdown_with_frontmatter(path)
    os_ui_language = SimpleNamespace(
        language_code=language_code,
        language_name=language_name,
    )

    rendered = replace_placeholders(
        command["body"],
        {"os_ui_language": os_ui_language},
        command["template_engine"],
    )

    assert expected in rendered
    assert command["commands"] == [
        {
            "name": "os_ui_language",
            "command": "functions/get_os_ui_language",
        }
    ]
    source = path.read_text(encoding="utf-8")
    assert "${1}" not in source
    assert "${2}" not in source
    assert "context.language_code" not in source
    assert "context.language_name" not in source


# --------------------------------------------------------------------------- #
# intelligence defaults and native policy
# --------------------------------------------------------------------------- #


def test_write_project_empty_intelligence_selection_keeps_template_defaults(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    SimpleProjectSetupService().write_project(
        _project_input(
            config_dir,
            llm_api_type="",
            cli_agent="",
            provider_api_keys={},
        )
    )

    model_mapping = load_yaml_file(config_dir / "intelligences/model_mapping.yml")
    cli_mapping = load_yaml_file(config_dir / "intelligences/cli_agent_mapping.yml")
    assert model_mapping["default"] == "models/openai/default.yml"
    assert cli_mapping["default"] == "cli_agents/codex/default.yml"


# --------------------------------------------------------------------------- #
# intelligence defaults on update
# --------------------------------------------------------------------------- #


def _seed_project(config_dir: Path) -> None:
    SimpleProjectSetupService().write_project(_project_input(config_dir))


def test_update_project_empty_intelligence_selection_preserves_existing_defaults(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    SimpleProjectSetupService().write_project(
        _project_input(
            config_dir,
            llm_api_type="gemini",
            cli_agent="claude",
            provider_api_keys={},
        )
    )

    SimpleProjectSetupService().update_project(
        ProjectSetupInput(
            config_dir=config_dir,
            language="en",
            llm_api_type="",
            cli_agent="",
        )
    )

    model_mapping = load_yaml_file(config_dir / "intelligences/model_mapping.yml")
    cli_mapping = load_yaml_file(config_dir / "intelligences/cli_agent_mapping.yml")
    assert model_mapping["default"] == "models/gemini/default.yml"
    assert cli_mapping["default"] == "cli_agents/claude/default.yml"


# --------------------------------------------------------------------------- #
# GitHub services: lane_map defaults
# --------------------------------------------------------------------------- #


def test_write_project_persists_default_lane_map(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    SimpleProjectSetupService().write_project(_github_project_input(config_dir))

    stored = load_yaml_file(config_dir / "team/project.yml")
    assert stored["services"]["ticket_service"]["lane_map"] == {
        "ready": "Todo",
        "working": "In Progress",
        "done": "Done",
    }


# --------------------------------------------------------------------------- #
# secret blank-update must not clear an existing secret
# --------------------------------------------------------------------------- #


def test_save_person_blank_secret_keeps_existing_value(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    service = SimplePersonSetupService()

    service.save_person(
        _person_input(config_dir, accounts=_tokens("github-token", "xoxb-token"))
    )
    assert _secrets()["ALICE_GITHUB_ACCESS_TOKEN"] == "github-token"

    # Update with blank secrets and an unrelated profile change.
    service.save_person(
        _person_input(
            config_dir,
            person_name="Alice Updated",
            roles=["reviewer"],
            speaking_style="style-b",
            accounts=_tokens(),
        )
    )

    env_values = _secrets()
    assert env_values["ALICE_GITHUB_ACCESS_TOKEN"] == "github-token"
    assert env_values["ALICE_SLACK_BOT_TOKEN"] == "xoxb-token"
    assert _identity()["name"] == "Alice Updated"
    assert _identity()["speaking_style"] == "style-b"
    assert _participation(config_dir)["profile"] == {"roles": {"reviewer": {}}}


# --------------------------------------------------------------------------- #
# member add / update / delete: env secret key added / updated / removed
# --------------------------------------------------------------------------- #


def test_member_crud_adds_updates_and_removes_env_keys(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    service = SimplePersonSetupService()

    # add (append): env key created.
    service.save_person(_person_input(config_dir, accounts=_tokens("token-v1")))
    assert _secrets()["ALICE_GITHUB_ACCESS_TOKEN"] == "token-v1"

    # update (same id): env key value replaced.
    service.save_person(_person_input(config_dir, accounts=_tokens("token-v2")))
    assert _secrets()["ALICE_GITHUB_ACCESS_TOKEN"] == "token-v2"

    # remove from the workspace: the identity keeps the key.
    service.remove_person(config_dir=config_dir, person_id="alice")
    assert not (config_dir / "team/members/alice").exists()
    assert _secrets()["ALICE_GITHUB_ACCESS_TOKEN"] == "token-v2"

    # delete the identity: secret key removed.
    service.delete_identity(person_id="alice")
    assert "ALICE_GITHUB_ACCESS_TOKEN" not in _secrets()
    assert not (identity_members_dir() / "alice").exists()


# --------------------------------------------------------------------------- #
# default command executor stored in team/project.yml
# --------------------------------------------------------------------------- #


def _default_person_id(config_dir: Path) -> str:
    project_data = load_yaml_file(config_dir / "team/project.yml")
    return str(project_data.get("default_person_id", ""))


def test_set_default_person_writes_and_clears_project_setting(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    _seed_project(config_dir)
    SimplePersonSetupService().save_person(_person_input(config_dir))
    service = SimpleProjectSetupService()

    result = service.set_default_person(config_dir=config_dir, person_id="alice")

    assert [str(file.path) for file in result.files] == [
        str(config_dir / "team/project.yml")
    ]
    assert _default_person_id(config_dir) == "alice"
    # Unrelated project settings survive the targeted update.
    assert load_yaml_file(config_dir / "team/project.yml")["language"] == "en"

    service.set_default_person(config_dir=config_dir, person_id="")

    assert _default_person_id(config_dir) == ""


def test_update_project_keeps_the_default_person(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    _seed_project(config_dir)
    SimplePersonSetupService().save_person(_person_input(config_dir))
    service = SimpleProjectSetupService()
    service.set_default_person(config_dir=config_dir, person_id="alice")

    service.update_project(
        ProjectSetupInput(config_dir=config_dir, language="ja", description="Ops")
    )

    project_data = load_yaml_file(config_dir / "team/project.yml")
    assert project_data["language"] == "ja"
    assert project_data["description"] == "Ops"
    assert _default_person_id(config_dir) == "alice"


def test_set_default_person_reports_no_change_when_already_set(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    _seed_project(config_dir)
    SimplePersonSetupService().save_person(_person_input(config_dir))
    service = SimpleProjectSetupService()
    service.set_default_person(config_dir=config_dir, person_id="alice")

    assert (
        service.set_default_person(config_dir=config_dir, person_id="alice").files == []
    )


def test_set_default_person_rejects_unknown_member(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    _seed_project(config_dir)

    with pytest.raises(Exception) as exc_info:
        SimpleProjectSetupService().set_default_person(
            config_dir=config_dir, person_id="ghost"
        )

    assert getattr(exc_info.value, "code", "") == "person_not_found"
    assert _default_person_id(config_dir) == ""


@pytest.mark.parametrize(
    "person_id",
    ["../members/alice", "alice/../alice", "Alice", "alice bob", "alice.md"],
)
def test_set_default_person_rejects_malformed_ids(
    tmp_path: Path, person_id: str
) -> None:
    # A person ID is also a directory name, so traversal and separators are
    # rejected before the file lookup can resolve them to a real member.
    config_dir = tmp_path / "config"
    _seed_project(config_dir)
    SimplePersonSetupService().save_person(_person_input(config_dir))

    with pytest.raises(Exception) as exc_info:
        SimpleProjectSetupService().set_default_person(
            config_dir=config_dir, person_id=person_id
        )

    assert getattr(exc_info.value, "code", "") == "invalid_person_id"
    assert _default_person_id(config_dir) == ""


def test_set_default_person_rejects_human_member(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    _seed_project(config_dir)
    SimplePersonSetupService().save_person(
        _person_input(
            config_dir,
            person_id="hana",
            person_type="human",
            person_name="Hana",
            git_email="3+hana@users.noreply.github.com",
        )
    )

    with pytest.raises(Exception) as exc_info:
        SimpleProjectSetupService().set_default_person(
            config_dir=config_dir, person_id="hana"
        )

    # A human member cannot execute commands, so it must not become the member
    # an omitted person resolves to.
    assert getattr(exc_info.value, "code", "") == "person_not_executable"
    assert _default_person_id(config_dir) == ""


def test_default_person_is_cleared_when_the_member_is_removed(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    _seed_project(config_dir)
    service = SimplePersonSetupService()
    service.save_person(_person_input(config_dir))
    service.save_person(
        _person_input(
            config_dir,
            person_id="bob",
            git_email="2+bob@users.noreply.github.com",
        )
    )
    SimpleProjectSetupService().set_default_person(
        config_dir=config_dir, person_id="bob"
    )

    service.remove_person(config_dir=config_dir, person_id="alice")
    assert _default_person_id(config_dir) == "bob"

    result = service.remove_person(config_dir=config_dir, person_id="bob")
    assert _default_person_id(config_dir) == ""
    assert [(file.path, file.action) for file in result.files] == [
        (config_dir / "team/members/bob" / MEMBER_FILE, "delete"),
        (config_dir / "team/project.yml", "update"),
    ]


# --------------------------------------------------------------------------- #
# task_schedules / routine_commands YAML round trip
# --------------------------------------------------------------------------- #


def test_person_routines_and_schedules_yaml_round_trip(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    service = SimplePersonSetupService()

    routine_commands = [
        "workflows/ticket_driven_workflow",
        "reports/daily_digest",
    ]
    task_schedules = [
        {"command": "reports/morning region=jp", "schedules": ["0 9 * * 1-5"]},
        {"command": "reports/evening", "schedules": ["0 18 * * *", "30 12 * * 6"]},
    ]

    service.save_person(
        _person_input(
            config_dir,
            routine_commands=routine_commands,
            task_schedules=task_schedules,
        )
    )

    # Raw YAML round trip: they are what the member does in this workspace.
    stored = _participation(config_dir)
    assert stored["routine_commands"] == routine_commands
    assert stored["task_schedules"] == task_schedules

    # Snapshot round trip.
    snapshot = service.read_person_config(config_dir=config_dir, person_id="alice")
    assert snapshot.routine_commands == routine_commands
    assert [s.model_dump() for s in snapshot.task_schedules] == task_schedules


# --------------------------------------------------------------------------- #
# character / relationships / speaking_style round trip
# --------------------------------------------------------------------------- #


def test_person_character_relationships_speaking_style_round_trip(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    service = SimplePersonSetupService()

    character = {
        "archetype": "strategic_pm",
        "traits": ["organized", "calm"],
        "conversation_preferences": {
            "join_when": ["planning", "review"],
            "avoid_when": ["off topic"],
            "contribution_style": ["clarify", "summarize"],
        },
    }
    service.save_person(
        _person_input(
            config_dir,
            speaking_style="Concise and direct.",
            relationships="Reports to {pm}; mentors juniors.",
            character=character,
        )
    )

    # Who the member is: the same in every workspace.
    stored = _identity()
    assert stored["speaking_style"] == "Concise and direct."
    assert stored["relationships"] == "Reports to {pm}; mentors juniors."
    assert stored["profile"]["character"] == character

    snapshot = service.read_person_config(config_dir=config_dir, person_id="alice")
    assert snapshot.speaking_style == "Concise and direct."
    assert snapshot.relationships == "Reports to {pm}; mentors juniors."
    assert snapshot.character == character
