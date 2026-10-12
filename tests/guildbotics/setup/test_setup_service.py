import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from guildbotics.integrations.provider import SetupError
from guildbotics.loader.yaml.yaml_team_loader import YamlTeamLoader
from guildbotics.setup import setup_service
from guildbotics.setup.setup_service import (
    MEMBER_SECRET_NAMES,
    PersonConfigSnapshot,
    PersonSetupInput,
    PersonUpdateInput,
    ProjectSetupInput,
    SimplePersonSetupService,
    SimpleProjectSetupService,
)
from guildbotics.utils.fileio import dump_yaml, load_yaml_file
from guildbotics.utils.secret_store import KeyringSecretStore

BOARD_URL = "https://github.com/orgs/GuildBotics/projects/1"


def _write_project(config_dir: Path, services: dict[str, Any]) -> None:
    SimpleProjectSetupService().write_project(
        ProjectSetupInput(
            config_dir=config_dir,
            language="en",
            llm_api_type="openai",
            cli_agent="codex",
            services=services,
        )
    )


def _stored_services(config_dir: Path) -> dict[str, Any] | None:
    return load_yaml_file(config_dir / "team/project.yml").get("services")


def test_workspace_creation_inside_exchange_is_refused_before_config_writes() -> None:
    from guildbotics.utils.workspace_state import registered_workspaces

    workspace = Path.home() / "Documents/GuildBotics/new-workspace"
    before = registered_workspaces()
    with pytest.raises(SetupError):
        SimpleProjectSetupService().write_project(
            ProjectSetupInput(
                config_dir=workspace / ".guildbotics/config",
                language="en",
                llm_api_type="openai",
            )
        )
    assert not workspace.exists()
    assert registered_workspaces() == before


def test_write_project_creates_cli_compatible_files(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"

    result = SimpleProjectSetupService().write_project(
        ProjectSetupInput(
            config_dir=config_dir,
            language="en",
            llm_api_type="openai",
            cli_agent="codex",
            provider_api_keys={"openai": "test-openai-key"},
        )
    )

    created_paths = {created_file.path for created_file in result.files}
    from guildbotics.utils.workspace_state import registered_workspaces

    assert tmp_path in registered_workspaces()
    assert config_dir / "team/project.yml" in created_paths
    assert config_dir / "intelligences/model_mapping.yml" in created_paths
    assert config_dir / "intelligences/cli_agent_mapping.yml" in created_paths
    assert config_dir / "commands/translate.md" in created_paths
    assert config_dir / "commands/summarize.md" in created_paths
    assert config_dir / "commands/get-time-of-day.yml" in created_paths
    assert config_dir / "commands/context-info.md" in created_paths
    assert KeyringSecretStore(config_dir).get("OPENAI_API_KEY") == "test-openai-key"


def test_write_project_without_services_creates_loadable_core_config(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".guildbotics/config"

    SimpleProjectSetupService().write_project(
        ProjectSetupInput(
            config_dir=config_dir,
            language="ja",
            description="Local automation workspace",
            llm_api_type="gemini",
            cli_agent="claude",
            provider_api_keys={"gemini": "test-google-key"},
        )
    )

    team = YamlTeamLoader(str(config_dir / "team")).load()

    assert team.project.get_language_code() == "ja"
    assert team.project.description == "Local automation workspace"
    assert team.project.services == {}
    assert _stored_services(config_dir) is None
    assert KeyringSecretStore(config_dir).get("GOOGLE_API_KEY") == "test-google-key"
    assert "OSのUI言語と英語の間で相互翻訳" in (
        config_dir / "commands/translate.md"
    ).read_text(encoding="utf-8")


def test_write_project_does_not_copy_samples_when_commands_exist(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    existing_command = config_dir / "commands/custom.md"
    existing_command.parent.mkdir(parents=True)
    existing_command.write_text("custom")

    result = SimpleProjectSetupService().write_project(
        ProjectSetupInput(
            config_dir=config_dir,
            language="ja",
            llm_api_type="gemini",
            cli_agent="claude",
            provider_api_keys={"gemini": "test-google-key"},
        )
    )

    created_paths = {created_file.path for created_file in result.files}
    assert existing_command.read_text() == "custom"
    assert config_dir / "commands/translate.md" not in created_paths
    assert not (config_dir / "commands/translate.md").exists()


# --------------------------------------------------------------------------- #
# project.services: each kind validated by the provider serving it
# --------------------------------------------------------------------------- #


def test_services_are_written_as_the_providers_validate_them(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"

    _write_project(
        config_dir,
        {
            "code_hosting_service": {"name": "GitHub", "owner": "GuildBotics"},
            "ticket_service": {
                "name": "GITHUB",
                "url": f" {BOARD_URL} ",
                "lane_map": {"ready": "Ready", "working": "", "done": "Shipped"},
            },
            "chat_service": {"name": "Slack"},
        },
    )

    # Names are the descriptors' own, the board's owner and number are read
    # off its URL, and a blank lane is the board's default.
    assert _stored_services(config_dir) == {
        "code_hosting_service": {"name": "github", "owner": "GuildBotics"},
        "ticket_service": {
            "name": "github",
            "url": BOARD_URL,
            "owner": "GuildBotics",
            "project_id": "1",
            "lane_map": {"ready": "Ready", "working": "In Progress", "done": "Shipped"},
        },
        "chat_service": {"name": "slack"},
    }
    snapshot = SimpleProjectSetupService().read_project_config(config_dir=config_dir)
    assert snapshot.services == _stored_services(config_dir)


def test_a_provider_without_a_setup_model_keeps_its_items(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"

    _write_project(
        config_dir,
        {"ticket_service": {"name": "Local", "tracker": "acme", "anything": [1, 2]}},
    )

    assert _stored_services(config_dir) == {
        "ticket_service": {"name": "local", "tracker": "acme", "anything": [1, 2]}
    }


@pytest.mark.parametrize(
    "services",
    [
        pytest.param({"chat_service": {"name": "github"}}, id="kind-not-served"),
        pytest.param({"ticket_service": {"name": "jira"}}, id="unknown-provider"),
        pytest.param({"ticket_service": {}}, id="no-provider"),
        pytest.param({"wiki_service": {"name": "github"}}, id="unknown-kind"),
        pytest.param(
            {"code_hosting_service": {"name": "github"}}, id="missing-required-item"
        ),
        pytest.param(
            {"code_hosting_service": {"name": "github", "owner": "a", "repo": "b"}},
            id="unknown-item",
        ),
        pytest.param(
            {"ticket_service": {"name": "github", "url": "https://github.com/acme"}},
            id="not-a-board-url",
        ),
        pytest.param(
            {
                "ticket_service": {
                    "name": "github",
                    "url": BOARD_URL,
                    "lane_map": {"ready": "Same", "done": "Same"},
                }
            },
            id="ready-equals-done",
        ),
        pytest.param({"chat_service": {"name": "slack", "token": "x"}}, id="extra"),
    ],
)
def test_services_a_provider_does_not_accept_are_refused(
    tmp_path: Path, services: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        ProjectSetupInput(config_dir=tmp_path, language="en", services=services)


def test_update_project_replaces_the_services_whole(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    _write_project(
        config_dir,
        {
            "code_hosting_service": {"name": "github", "owner": "GuildBotics"},
            "ticket_service": {"name": "github", "url": BOARD_URL},
            "chat_service": {"name": "slack", "base_url": "https://proxy.example"},
        },
    )
    service = SimpleProjectSetupService()
    base: dict[str, Any] = {"config_dir": config_dir, "language": "en"}

    service.update_project(
        ProjectSetupInput(**base, services={"chat_service": {"name": "slack"}})
    )
    assert _stored_services(config_dir) == {"chat_service": {"name": "slack"}}

    service.update_project(ProjectSetupInput(**base))
    assert _stored_services(config_dir) is None
    assert service.read_project_config(config_dir=config_dir).services == {}


def test_update_project_is_non_destructive_for_env_and_cli_agents(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    KeyringSecretStore(config_dir).set("OPENAI_API_KEY", "existing-openai")
    cli_agents_dir = config_dir / "intelligences/cli_agents"
    cli_agents_dir.mkdir(parents=True, exist_ok=True)
    custom_cli_file = cli_agents_dir / "custom.yml"
    custom_cli_file.write_text("script: echo custom")

    SimpleProjectSetupService().update_project(
        ProjectSetupInput(
            config_dir=config_dir,
            language="ja",
            description="Updated",
            llm_api_type="gemini",
            cli_agent="claude",
        )
    )

    assert custom_cli_file.read_text() == "script: echo custom"
    assert (config_dir / "commands/translate.md").exists()
    assert "OSのUI言語と英語の間で相互翻訳" in (
        config_dir / "commands/translate.md"
    ).read_text(encoding="utf-8")
    assert KeyringSecretStore(config_dir).get("OPENAI_API_KEY") == "existing-openai"
    assert not (tmp_path / ".env").exists()


# --------------------------------------------------------------------------- #
# members: accounts, chat channels, defaults
# --------------------------------------------------------------------------- #


def _person(config_dir: Path, **overrides: Any) -> PersonSetupInput:
    payload: dict[str, Any] = {
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


def _stored_person(config_dir: Path, person_id: str = "alice") -> dict[str, Any]:
    return load_yaml_file(config_dir / f"team/members/{person_id}/person.yml")


def test_write_person_creates_person_config_and_masks_secrets(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"

    result = SimplePersonSetupService().write_person(
        _person(
            config_dir,
            person_id="alice-bot",
            person_name="Alice Bot",
            git_email="1+alice-bot@users.noreply.github.com",
            roles=["architect", "project_manager"],
            speaking_style="Professional.",
            character={
                "archetype": "strategic_project_manager_architect",
                "traits": ["strategic", "organized"],
                "conversation_preferences": {
                    "join_when": ["Architecture discussion"],
                    "avoid_when": ["Pure small talk"],
                    "contribution_style": ["Clarify decisions"],
                },
            },
            accounts={
                "github": {
                    "account_type": "machine_user",
                    "username": "alice-bot",
                    "access_token": "secret-token",
                }
            },
        )
    )

    person_file = config_dir / "team/members/alice-bot/person.yml"
    assert person_file in {created_file.path for created_file in result.files}
    person = _stored_person(config_dir, "alice-bot")
    assert person["person_id"] == "alice-bot"
    assert set(person["profile"]["roles"]) == {"architect", "project_manager"}
    assert (
        person["profile"]["character"]["archetype"]
        == "strategic_project_manager_architect"
    )
    assert person["account_info"] == {
        "git_user": "Alice Bot",
        "git_email": "1+alice-bot@users.noreply.github.com",
        "github_account_type": "machine_user",
        "github_username": "alice-bot",
    }
    assert "secret-token" not in person_file.read_text()
    assert (
        KeyringSecretStore(config_dir).get("ALICE_BOT_GITHUB_ACCESS_TOKEN")
        == "secret-token"
    )
    assert not (tmp_path / ".env").exists()


def test_account_info_merges_every_providers_account(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"

    SimplePersonSetupService().write_person(
        _person(
            config_dir,
            accounts={
                "github": {
                    "account_type": "github_apps",
                    "username": "alice[bot]",
                    "app_id": 7,
                    "installation_id": 42,
                },
                "slack": {"bot_token": "xoxb-a"},
            },
        )
    )

    assert _stored_person(config_dir)["account_info"] == {
        "git_user": "Alice",
        "git_email": "1+alice@users.noreply.github.com",
        "github_account_type": "github_apps",
        "github_username": "alice[bot]",
        "github_app_id": "7",
        "github_installation_id": "42",
    }


@pytest.mark.parametrize(
    "accounts",
    [
        pytest.param({"jira": {}}, id="unknown-provider"),
        pytest.param({"local": {}}, id="provider-without-member-account"),
        pytest.param({"slack": {"password": "x"}}, id="unknown-field"),
        pytest.param({"github": {"account_type": "robot"}}, id="invalid-value"),
    ],
)
def test_accounts_a_provider_does_not_accept_are_refused(
    tmp_path: Path, accounts: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        _person(tmp_path, accounts=accounts)


def test_chat_channels_are_named_as_the_projects_chat_names_them(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    _write_project(config_dir, {"chat_service": {"name": "slack"}})

    SimplePersonSetupService().write_person(
        _person(
            config_dir,
            chat_channels=["#general", "C0123456789"],
            chat_channel_participation={"general": "strict", "C0123456789": "social"},
        )
    )

    assert _stored_person(config_dir)["message_channels"] == [
        {
            "name": "general",
            "used_as": ["internal_communication"],
            "used_by": ["alice"],
            "channel_info": {},
            "chat": {
                "enabled": True,
                "participation": "strict",
                "channel_name": "general",
            },
        },
        {
            "name": "C0123456789",
            "used_as": ["internal_communication"],
            "used_by": ["alice"],
            "channel_info": {"channel_id": "C0123456789"},
            "chat": {
                "enabled": True,
                "participation": "social",
                "channel_id": "C0123456789",
            },
        },
    ]


@pytest.mark.parametrize("services", [{}, {"chat_service": {"name": "local"}}])
def test_chat_channels_are_names_without_a_chat_that_reads_ids(
    tmp_path: Path, services: dict[str, Any]
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    _write_project(config_dir, services)

    SimplePersonSetupService().write_person(
        _person(config_dir, chat_channels=["#C0123456789"])
    )

    [channel] = _stored_person(config_dir)["message_channels"]
    assert channel["channel_info"] == {}
    assert channel["chat"] == {
        "enabled": True,
        "participation": "strict",
        "channel_name": "C0123456789",
    }


def test_write_human_person_clears_agent_execution_settings(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"

    SimplePersonSetupService().write_person(
        _person(
            config_dir,
            person_type="human",
            git_email="alice@example.com",
            roles=["product"],
            speaking_style="Legacy style",
            relationships="Legacy relationship",
            character={"archetype": "legacy"},
            accounts={
                "github": {
                    "account_type": "machine_user",
                    "username": "alice",
                    "access_token": "secret-token",
                },
                "slack": {
                    "user_id": "U012345678",
                    "bot_token": "xoxb-secret",
                    "app_token": "xapp-secret",
                },
            },
            chat_channels=["general"],
            routine_commands=["workflows/ticket_driven_workflow"],
            task_schedules=[
                setup_service.PersonTaskScheduleInput(
                    command="workflows/ticket_driven_workflow",
                    schedules=["0 * * * *"],
                )
            ],
        )
    )

    person = _stored_person(config_dir)
    assert person["is_active"] is False
    assert person["account_info"] == {
        "git_user": "Alice",
        "git_email": "alice@example.com",
        "github_account_type": "human",
        "github_username": "alice",
        "slack_user_id": "U012345678",
    }
    assert person.get("speaking_style", "") == ""
    assert person.get("relationships", "") == ""
    assert person["profile"] == {"roles": {"product": {}}}
    assert "message_channels" not in person
    assert "routine_commands" not in person
    assert "task_schedules" not in person
    assert KeyringSecretStore(config_dir).keys() == []


def test_write_person_handles_braces_in_free_text(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"

    SimplePersonSetupService().write_person(
        _person(
            config_dir,
            person_id="brace-user",
            person_name="Brace {User}",
            speaking_style="Use {structured} notes",
            relationships="Depends on {pm}",
        )
    )

    person = _stored_person(config_dir, "brace-user")
    assert person["name"] == "Brace {User}"
    assert person["speaking_style"] == "Use {structured} notes"
    assert person["relationships"] == "Depends on {pm}"


def test_read_person_config_exposes_avatar_timestamp(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    service = SimplePersonSetupService()
    service.write_person(_person(config_dir))

    # Without an avatar file the timestamp defaults to 0.
    snapshot = service.read_person_config(config_dir=config_dir, person_id="alice")
    assert snapshot.avatar_timestamp == 0

    # Once an avatar file exists, its mtime is exposed as an int timestamp.
    avatar_path = config_dir / "team/members/alice/avatar.png"
    avatar_path.write_bytes(b"image-bytes")
    snapshot = service.read_person_config(config_dir=config_dir, person_id="alice")
    assert snapshot.avatar_timestamp == int(avatar_path.stat().st_mtime)


def test_read_person_config_shows_each_providers_account(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    _write_project(config_dir, {"chat_service": {"name": "slack"}})
    service = SimplePersonSetupService()
    service.write_person(
        _person(
            config_dir,
            character={"archetype": "creative_designer"},
            accounts={
                "github": {
                    "account_type": "github_apps",
                    "username": "alice[bot]",
                    "app_id": 456,
                    "installation_id": 123,
                    "access_token": "token-a",
                },
                "slack": {"bot_token": "xoxb-a", "app_token": "xapp-a"},
            },
            chat_channels=["C1"],
            chat_channel_participation={"C1": "muted"},
        )
    )

    snapshot: PersonConfigSnapshot = service.read_person_config(
        config_dir=config_dir, person_id="alice"
    )

    assert snapshot.git_email == "1+alice@users.noreply.github.com"
    assert snapshot.accounts == {
        "github": {
            "account_type": "github_apps",
            "username": "alice[bot]",
            "installation_id": 123,
            "app_id": 456,
            "has_private_key": False,
            "has_access_token": True,
        },
        "slack": {"user_id": "", "has_bot_token": True, "has_app_token": True},
    }
    assert snapshot.secret_env_keys == {
        name: f"ALICE_{name}" for name in MEMBER_SECRET_NAMES
    }
    assert snapshot.chat_channels == ["C1"]
    assert snapshot.chat_channel_participation == {"C1": "muted"}
    assert snapshot.character == {"archetype": "creative_designer"}


def test_member_config_round_trips_patrol_settings(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    service = SimplePersonSetupService()

    service.write_person(
        _person(
            config_dir,
            routine_commands=["workflows/ticket_driven_workflow"],
            task_schedules=[
                {
                    "command": "reports/morning_summary region=jp",
                    "schedules": ["0 9 * * 1-5", "30 14 * * *"],
                }
            ],
        )
    )

    snapshot = service.read_person_config(config_dir=config_dir, person_id="alice")

    assert snapshot.routine_commands == ["workflows/ticket_driven_workflow"]
    assert [schedule.model_dump() for schedule in snapshot.task_schedules] == [
        {
            "command": "reports/morning_summary region=jp",
            "schedules": ["0 9 * * 1-5", "30 14 * * *"],
        }
    ]

    service.update_person(
        PersonUpdateInput(
            **_person(config_dir, routine_commands=[], task_schedules=[]).model_dump(),
            original_person_id="alice",
        )
    )

    updated = _stored_person(config_dir)
    assert "routine_commands" not in updated
    assert "task_schedules" not in updated


def test_write_person_seeds_default_patrol_when_the_project_has_tickets(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    _write_project(config_dir, {"ticket_service": {"name": "local"}})

    SimplePersonSetupService().write_person(_person(config_dir))

    assert _stored_person(config_dir)["routine_commands"] == [
        "workflows/ticket_driven_workflow"
    ]


@pytest.mark.parametrize(
    "services",
    [
        pytest.param(None, id="no-project"),
        pytest.param({"code_hosting_service": {"name": "local"}}, id="no-tickets"),
    ],
)
def test_write_person_does_not_seed_default_patrol_without_tickets(
    tmp_path: Path, services: dict[str, Any] | None
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    if services is not None:
        _write_project(config_dir, services)

    SimplePersonSetupService().write_person(
        _person(
            config_dir,
            accounts={"github": {"account_type": "machine_user", "username": "a"}},
        )
    )

    assert "routine_commands" not in _stored_person(config_dir)


def test_update_person_does_not_seed_default_patrol(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    _write_project(config_dir, {"ticket_service": {"name": "local"}})
    service = SimplePersonSetupService()
    service.write_person(_person(config_dir, routine_commands=["reports/daily"]))

    service.update_person(
        PersonUpdateInput(
            **_person(config_dir, routine_commands=[]).model_dump(),
            original_person_id="alice",
        )
    )

    assert "routine_commands" not in _stored_person(config_dir)


def test_write_person_omits_patrol_and_schedule_for_human(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    _write_project(config_dir, {"ticket_service": {"name": "local"}})

    SimplePersonSetupService().write_person(
        _person(
            config_dir,
            person_type="human",
            is_active=False,
            routine_commands=["workflows/ticket_driven_workflow"],
            task_schedules=[{"command": "reports/morning", "schedules": ["0 9 * * *"]}],
        )
    )

    stored = _stored_person(config_dir)
    assert "routine_commands" not in stored
    assert "task_schedules" not in stored


def test_read_person_config_skips_invalid_task_schedules(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    service = SimplePersonSetupService()
    service.write_person(_person(config_dir))
    person_file = config_dir / "team/members/alice/person.yml"
    person_data = load_yaml_file(person_file)
    person_data["task_schedules"] = [
        {"command": "reports/morning_summary", "schedules": ["0 9 * * 1-5"]},
        {"command": "bad", "schedules": ["invalid cron"]},
        "not a schedule",
    ]
    # Hand-written fixture content simulating a user's manual edit; no product
    # writer produces it, so it is written directly rather than through one.
    person_file.write_text(dump_yaml(person_data), encoding="utf-8")

    snapshot = service.read_person_config(config_dir=config_dir, person_id="alice")

    assert [schedule.model_dump() for schedule in snapshot.task_schedules] == [
        {"command": "reports/morning_summary", "schedules": ["0 9 * * 1-5"]}
    ]


def test_update_rename_and_delete_follow_the_member(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    service = SimplePersonSetupService()
    service.write_person(
        _person(
            config_dir,
            accounts={
                "github": {
                    "account_type": "machine_user",
                    "username": "alice",
                    "access_token": "token-a",
                }
            },
        )
    )

    service.update_person(
        PersonUpdateInput(
            **_person(
                config_dir,
                person_id="alice-renamed",
                person_name="Alice Updated",
                is_active=False,
                roles=["reviewer"],
                character={"archetype": "strategic_pm"},
                accounts={
                    "github": {
                        "account_type": "machine_user",
                        "username": "alice-renamed",
                        "access_token": "token-b",
                    },
                    "slack": {"bot_token": "xoxb-b", "app_token": "xapp-b"},
                },
            ).model_dump(),
            original_person_id="alice",
        )
    )

    renamed = _stored_person(config_dir, "alice-renamed")
    assert not (config_dir / "team/members/alice").exists()
    assert renamed["person_id"] == "alice-renamed"
    assert renamed["name"] == "Alice Updated"
    assert renamed["account_info"]["github_username"] == "alice-renamed"
    assert renamed["profile"]["character"] == {"archetype": "strategic_pm"}
    store = KeyringSecretStore(config_dir)
    assert sorted(store.keys()) == [
        "ALICE_RENAMED_GITHUB_ACCESS_TOKEN",
        "ALICE_RENAMED_SLACK_APP_TOKEN",
        "ALICE_RENAMED_SLACK_BOT_TOKEN",
    ]
    assert store.get("ALICE_RENAMED_GITHUB_ACCESS_TOKEN") == "token-b"
    assert store.get("ALICE_RENAMED_SLACK_BOT_TOKEN") == "xoxb-b"

    service.delete_person(config_dir=config_dir, person_id="alice-renamed")

    assert not (config_dir / "team/members/alice-renamed").exists()
    assert store.keys() == []


def test_intelligence_templates_are_copied_byte_for_byte(
    tmp_path: Path, monkeypatch
) -> None:
    """Reading a template as text and writing it back rewrites its newlines to
    the ones this OS uses, so the same template landed as CRLF on Windows and
    LF elsewhere. Every device that joined a hub then found these files
    differing -- a difference nobody made, in files nobody edited."""
    templates = tmp_path / "templates"
    shutil.copytree(setup_service.get_template_path(), templates)
    crlf = sorted((templates / "intelligences/cli_agents").glob("*/*.yml"))
    for template in crlf:
        template.write_bytes(template.read_bytes().replace(b"\n", b"\r\n"))
    monkeypatch.setattr(setup_service, "get_template_path", lambda: templates)
    config_dir = tmp_path / ".guildbotics/config"

    SimpleProjectSetupService().write_project(
        ProjectSetupInput(
            config_dir=config_dir,
            language="en",
            llm_api_type="openai",
            cli_agent="codex",
        )
    )

    for template in crlf:
        copied = config_dir / template.relative_to(templates)
        assert copied.read_bytes() == template.read_bytes(), copied


def test_a_stored_provider_name_is_read_as_the_runtime_reads_it(tmp_path: Path) -> None:
    # The runtime reads a provider's name case-insensitively, and so does the
    # screen that edits it.
    config_dir = tmp_path / "config"
    _write_project(config_dir, {"chat_service": {"name": "slack"}})
    project_file = config_dir / "team/project.yml"
    project_file.write_text(
        project_file.read_text().replace("name: slack", "name: Slack"),
        encoding="utf-8",
    )

    snapshot = SimpleProjectSetupService().read_project_config(config_dir=config_dir)

    assert snapshot.services == {"chat_service": {"name": "slack"}}


def test_chat_channels_and_their_policies_are_named_without_the_hash(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    _write_project(config_dir, {"chat_service": {"name": "slack"}})

    SimplePersonSetupService().write_person(
        _person(
            config_dir,
            chat_channels=[" #general ", "  "],
            chat_channel_participation={"#general": "social"},
        )
    )

    [channel] = _stored_person(config_dir)["message_channels"]
    assert channel["name"] == "general"
    assert channel["chat"]["participation"] == "social"


def test_a_saved_owner_scopes_the_repositories_its_form_named(tmp_path: Path) -> None:
    # The form's checks look past the whitespace around an owner; the stored
    # owner must be the one the write scope compares repositories with.
    from guildbotics.integrations.repository_scope import in_scope

    config_dir = tmp_path / "config"
    _write_project(
        config_dir,
        {"code_hosting_service": {"name": "github", "owner": " GuildBotics "}},
    )

    owner = _stored_services(config_dir)["code_hosting_service"]["owner"]
    assert owner == "GuildBotics"
    assert in_scope(owner, "GuildBotics/GuildBotics")
    with pytest.raises(ValidationError):
        ProjectSetupInput(
            config_dir=config_dir,
            language="en",
            services={"code_hosting_service": {"name": "github", "owner": "  "}},
        )


def test_a_pasted_api_key_is_stored_without_its_surrounding_whitespace(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    SimpleProjectSetupService().write_project(
        ProjectSetupInput(
            config_dir=config_dir,
            language="en",
            llm_api_type="openai",
            cli_agent="codex",
            provider_api_keys={"openai": " sk-test\n"},
        )
    )

    assert KeyringSecretStore(config_dir).get("OPENAI_API_KEY") == "sk-test"
