from pathlib import Path

from guildbotics.editions.simple import setup_service
from guildbotics.editions.simple.setup_service import (
    PersonConfigSnapshot,
    PersonSetupInput,
    PersonUpdateInput,
    ProjectSetupInput,
    ProjectUpdateInput,
    SimplePersonSetupService,
    SimpleProjectSetupService,
)
from guildbotics.loader.yaml.yaml_team_loader import YamlTeamLoader
from guildbotics.utils.fileio import load_yaml_file, save_yaml_file


def test_write_project_creates_cli_compatible_files(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file_path = tmp_path / ".env"

    result = SimpleProjectSetupService().write_project(
        ProjectSetupInput(
            config_dir=config_dir,
            env_file_path=env_file_path,
            env_file_option="overwrite",
            language="en",
            owner="GuildBotics",
            project_id="1",
            github_project_url="https://github.com/orgs/GuildBotics/projects/1",
            llm_api_type="openai",
            cli_agent="codex",
            provider_api_keys={"openai": "test-openai-key"},
        )
    )

    created_paths = {created_file.path for created_file in result.files}
    assert config_dir / "team/project.yml" in created_paths
    assert config_dir / "intelligences/model_mapping.yml" in created_paths
    assert config_dir / "intelligences/cli_agent_mapping.yml" in created_paths
    assert config_dir / "commands/translate.md" in created_paths
    assert config_dir / "commands/summarize.md" in created_paths
    assert config_dir / "commands/get-time-of-day.yml" in created_paths
    assert config_dir / "commands/context-info.md" in created_paths
    assert env_file_path in created_paths
    assert "OPENAI_API_KEY=test-openai-key" in env_file_path.read_text()


def test_write_project_without_github_creates_loadable_core_config(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file_path = tmp_path / ".env"

    SimpleProjectSetupService().write_project(
        ProjectSetupInput(
            config_dir=config_dir,
            env_file_path=env_file_path,
            env_file_option="overwrite",
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
    assert "GOOGLE_API_KEY=test-google-key" in env_file_path.read_text()
    assert (
        "OSのUI言語と英語の間で相互翻訳"
        in (config_dir / "commands/translate.md").read_text()
    )


def test_write_project_does_not_copy_samples_when_commands_exist(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file_path = tmp_path / ".env"
    existing_command = config_dir / "commands/custom.md"
    existing_command.parent.mkdir(parents=True)
    existing_command.write_text("custom")

    result = SimpleProjectSetupService().write_project(
        ProjectSetupInput(
            config_dir=config_dir,
            env_file_path=env_file_path,
            env_file_option="overwrite",
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


def test_project_service_parses_github_urls() -> None:
    service = SimpleProjectSetupService()

    project = service.parse_github_project_url(
        "https://github.com/orgs/GuildBotics/projects/12?pane=info"
    )

    assert project.owner == "GuildBotics"
    assert project.project_id == "12"
    assert project.url == "https://github.com/orgs/GuildBotics/projects/12"


def test_write_person_creates_person_config_and_masks_secrets(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file_path = tmp_path / ".env"
    env_file_path.write_text("OPENAI_API_KEY=existing")

    result = SimplePersonSetupService().write_person(
        PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file_path,
            append_env_file=True,
            person_type="machine_user",
            person_id="alice-bot",
            person_name="Alice Bot",
            is_active=True,
            github_username="alice-bot",
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
            github_access_token="secret-token",
        )
    )

    person_file = config_dir / "team/members/alice-bot/person.yml"
    assert person_file in {created_file.path for created_file in result.files}
    assert "person_id: alice-bot" in person_file.read_text()
    assert "  architect:" in person_file.read_text()
    assert "archetype: strategic_project_manager_architect" in person_file.read_text()
    assert "ALICE_BOT_GITHUB_ACCESS_TOKEN=secret-token" in env_file_path.read_text()
    assert result.masked_environment_variables == [
        "ALICE_BOT_GITHUB_ACCESS_TOKEN=********"
    ]


def test_write_person_accepts_slack_channel_names_and_ids(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file_path = tmp_path / ".env"
    env_file_path.write_text("")

    SimplePersonSetupService().write_person(
        PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file_path,
            append_env_file=False,
            person_type="machine_user",
            person_id="alice-bot",
            person_name="Alice Bot",
            is_active=True,
            github_username="alice-bot",
            git_email="1+alice-bot@users.noreply.github.com",
            roles=["architect"],
            speaking_style="Professional.",
            slack_channels=["#general", "C0123456789"],
            slack_channel_participation={
                "general": "strict",
                "C0123456789": "social",
            },
        )
    )

    person = load_yaml_file(config_dir / "team/members/alice-bot/person.yml")
    channels = person["message_channels"]

    assert channels[0]["name"] == "general"
    assert channels[0]["chat"]["enabled"] is True
    assert channels[0]["chat"]["channel_name"] == "general"
    assert channels[0]["chat"]["participation"] == "strict"
    assert channels[1]["name"] == "C0123456789"
    assert channels[1]["chat"]["enabled"] is True
    assert channels[1]["chat"]["channel_id"] == "C0123456789"
    assert channels[1]["chat"]["participation"] == "social"
    assert channels[1]["channel_info"]["channel_id"] == "C0123456789"


def test_write_human_person_clears_agent_execution_settings(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file_path = tmp_path / ".env"
    env_file_path.write_text("")

    result = SimplePersonSetupService().write_person(
        PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file_path,
            append_env_file=True,
            person_type="human",
            github_account_type="machine_user",
            person_id="alice",
            person_name="Alice",
            is_active=True,
            github_username="alice",
            git_email="alice@example.com",
            roles=["product"],
            speaking_style="Legacy style",
            relationships="Legacy relationship",
            character={"archetype": "legacy"},
            github_access_token="secret-token",
            slack_user_id="U012345678",
            slack_bot_token="xoxb-secret",
            slack_app_token="xapp-secret",
            slack_channels=["general"],
            routine_commands=["workflows/ticket_driven_workflow"],
            task_schedules=[
                setup_service.PersonTaskScheduleInput(
                    command="workflows/ticket_driven_workflow",
                    schedules=["0 * * * *"],
                )
            ],
        )
    )

    person = load_yaml_file(config_dir / "team/members/alice/person.yml")
    assert person["is_active"] is False
    assert person["account_info"]["github_account_type"] == "human"
    assert person["account_info"]["slack_user_id"] == "U012345678"
    assert person.get("speaking_style", "") == ""
    assert person.get("relationships", "") == ""
    assert person["profile"] == {"roles": {"product": {}}}
    assert "message_channels" not in person
    assert "routine_commands" not in person
    assert "task_schedules" not in person
    assert env_file_path.read_text() == ""
    assert result.masked_environment_variables == []


def test_person_service_parses_github_apps_url() -> None:
    app_name = SimplePersonSetupService().parse_github_apps_url(
        "https://github.com/organizations/GuildBotics/settings/apps/guildbotics-agent"
    )

    assert app_name == "guildbotics-agent"


def test_person_service_resolves_github_user(monkeypatch) -> None:
    github_user_id = 123

    class ResponseStub:
        status_code = 200

        def json(self):
            return {"id": github_user_id}

    monkeypatch.setattr(
        setup_service.requests, "get", lambda url, timeout: ResponseStub()
    )

    reference = SimplePersonSetupService().resolve_github_user("alice@example.com")

    assert reference.person_id == "alice"
    assert reference.github_username == "alice"
    assert reference.github_user_id == github_user_id
    assert reference.git_email == "123+alice@users.noreply.github.com"


def test_write_person_handles_braces_in_free_text(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file_path = tmp_path / ".env"
    env_file_path.write_text("")

    result = SimplePersonSetupService().write_person(
        PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file_path,
            append_env_file=False,
            person_type="machine_user",
            person_id="brace-user",
            person_name="Brace {User}",
            is_active=True,
            github_username="brace-user",
            git_email="1+brace-user@users.noreply.github.com",
            roles=["architect"],
            speaking_style="Use {structured} notes",
            relationships="Depends on {pm}",
        )
    )

    person_file = config_dir / "team/members/brace-user/person.yml"
    assert person_file in {created_file.path for created_file in result.files}
    text = person_file.read_text()
    assert "person_id: brace-user" in text
    assert "{structured}" in text
    assert "{pm}" in text


def test_update_project_is_non_destructive_for_env_and_cli_agents(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=existing-openai\nEXTRA=value")
    team_dir = config_dir / "team"
    team_dir.mkdir(parents=True, exist_ok=True)
    (team_dir / "project.yml").write_text(
        "\n".join(
            [
                "language: en",
                "description: Existing",
                "services:",
                "  code_hosting_service:",
                "    name: GitHub",
                "    owner: GuildBotics",
                "    repo_base_url: https://github.com",
            ]
        )
    )
    cli_agents_dir = config_dir / "intelligences/cli_agents"
    cli_agents_dir.mkdir(parents=True, exist_ok=True)
    custom_cli_file = cli_agents_dir / "custom.yml"
    custom_cli_file.write_text("script: echo custom")

    SimpleProjectSetupService().update_project(
        ProjectUpdateInput(
            config_dir=config_dir,
            env_file_path=env_file,
            language="ja",
            description="Updated",
            llm_api_type="gemini",
            cli_agent="claude",
            github_enabled=False,
        )
    )

    assert custom_cli_file.read_text() == "script: echo custom"
    assert (config_dir / "commands/translate.md").exists()
    assert (
        "OSのUI言語と英語の間で相互翻訳"
        in (config_dir / "commands/translate.md").read_text()
    )
    env_text = env_file.read_text()
    assert "OPENAI_API_KEY=existing-openai" in env_text
    assert "EXTRA=value" in env_text


def test_read_person_config_exposes_avatar_timestamp(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file = tmp_path / ".env"
    env_file.write_text("")
    service = SimplePersonSetupService()
    service.write_person(
        PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file,
            append_env_file=False,
            person_type="machine_user",
            person_id="alice",
            person_name="Alice",
            is_active=True,
            github_username="alice",
            git_email="1+alice@users.noreply.github.com",
            roles=["architect"],
            speaking_style="style-a",
        )
    )

    # Without an avatar file the timestamp defaults to 0.
    snapshot = service.read_person_config(
        config_dir=config_dir, person_id="alice", env_file_path=env_file
    )
    assert snapshot.avatar_timestamp == 0

    # Once an avatar file exists, its mtime is exposed as an int timestamp.
    avatar_path = config_dir / "team/members/alice/avatar.png"
    avatar_path.write_bytes(b"image-bytes")
    snapshot = service.read_person_config(
        config_dir=config_dir, person_id="alice", env_file_path=env_file
    )
    assert snapshot.avatar_timestamp == int(avatar_path.stat().st_mtime)


def test_member_read_update_delete_with_slack(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
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
    service = SimplePersonSetupService()
    service.write_person(
        PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file,
            append_env_file=False,
            person_type="machine_user",
            person_id="alice",
            person_name="Alice",
            is_active=True,
            github_username="alice",
            git_email="1+alice@users.noreply.github.com",
            roles=["architect"],
            speaking_style="style-a",
            relationships="rel-a",
            character={
                "archetype": "creative_designer",
                "traits": ["creative"],
                "interests": ["ux"],
            },
            github_access_token="token-a",
            slack_bot_token="xoxb-a",
            slack_app_token="xapp-a",
            slack_channels=["C1"],
            slack_channel_participation={"C1": "muted"},
        )
    )

    snapshot: PersonConfigSnapshot = service.read_person_config(
        config_dir=config_dir,
        person_id="alice",
        env_file_path=env_file,
    )
    assert snapshot.person_id == "alice"
    assert snapshot.has_github_access_token is True
    assert snapshot.has_slack_bot_token is True
    assert snapshot.slack_channels == ["C1"]
    assert snapshot.slack_channel_participation == {"C1": "muted"}
    assert snapshot.character["archetype"] == "creative_designer"


def test_member_config_round_trips_patrol_settings(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file = tmp_path / ".env"
    service = SimplePersonSetupService()

    service.write_person(
        PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file,
            append_env_file=False,
            person_type="machine_user",
            person_id="alice",
            person_name="Alice",
            is_active=True,
            github_username="alice",
            git_email="1+alice@users.noreply.github.com",
            roles=["architect"],
            speaking_style="style-a",
            routine_commands=["workflows/ticket_driven_workflow"],
            task_schedules=[
                {
                    "command": "reports/morning_summary region=jp",
                    "schedules": ["0 9 * * 1-5", "30 14 * * *"],
                }
            ],
        )
    )

    snapshot = service.read_person_config(
        config_dir=config_dir,
        person_id="alice",
        env_file_path=env_file,
    )

    assert snapshot.routine_commands == ["workflows/ticket_driven_workflow"]
    assert [schedule.model_dump() for schedule in snapshot.task_schedules] == [
        {
            "command": "reports/morning_summary region=jp",
            "schedules": ["0 9 * * 1-5", "30 14 * * *"],
        }
    ]

    service.update_person(
        PersonUpdateInput(
            config_dir=config_dir,
            env_file_path=env_file,
            original_person_id="alice",
            append_env_file=False,
            person_type="machine_user",
            person_id="alice",
            person_name="Alice",
            is_active=True,
            github_username="alice",
            git_email="1+alice@users.noreply.github.com",
            roles=["architect"],
            speaking_style="style-a",
            routine_commands=[],
            task_schedules=[],
        )
    )

    updated = load_yaml_file(config_dir / "team/members/alice/person.yml")
    assert "routine_commands" not in updated
    assert "task_schedules" not in updated


def test_write_person_seeds_default_patrol_for_github_agent(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file = tmp_path / ".env"
    service = SimplePersonSetupService()

    service.write_person(
        PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file,
            append_env_file=False,
            person_type="machine_user",
            person_id="alice",
            person_name="Alice",
            is_active=True,
            github_account_type="machine_user",
            github_username="alice",
            git_email="1+alice@users.noreply.github.com",
            roles=["architect"],
            speaking_style="style-a",
        )
    )

    stored = load_yaml_file(config_dir / "team/members/alice/person.yml")

    assert stored["routine_commands"] == ["workflows/ticket_driven_workflow"]


def test_write_person_does_not_seed_default_patrol_without_github(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file = tmp_path / ".env"
    service = SimplePersonSetupService()

    service.write_person(
        PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file,
            append_env_file=False,
            person_type="agent",
            person_id="alice",
            person_name="Alice",
            is_active=True,
            github_account_type="none",
            github_username="",
            git_email="",
            roles=["architect"],
            speaking_style="style-a",
        )
    )

    stored = load_yaml_file(config_dir / "team/members/alice/person.yml")

    assert "routine_commands" not in stored


def test_write_person_omits_patrol_and_schedule_for_human(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file = tmp_path / ".env"
    service = SimplePersonSetupService()

    service.write_person(
        PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file,
            append_env_file=False,
            person_type="human",
            person_id="alice",
            person_name="Alice",
            is_active=False,
            github_account_type="human",
            github_username="",
            git_email="",
            roles=["architect"],
            speaking_style="style-a",
            routine_commands=["workflows/ticket_driven_workflow"],
            task_schedules=[{"command": "reports/morning", "schedules": ["0 9 * * *"]}],
        )
    )

    stored = load_yaml_file(config_dir / "team/members/alice/person.yml")

    assert "routine_commands" not in stored
    assert "task_schedules" not in stored


def test_read_person_config_skips_invalid_task_schedules(tmp_path: Path) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file = tmp_path / ".env"
    service = SimplePersonSetupService()
    service.write_person(
        PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file,
            append_env_file=False,
            person_type="machine_user",
            person_id="alice",
            person_name="Alice",
            is_active=True,
            github_username="alice",
            git_email="1+alice@users.noreply.github.com",
            roles=["architect"],
            speaking_style="style-a",
        )
    )
    person_file = config_dir / "team/members/alice/person.yml"
    person_data = load_yaml_file(person_file)
    person_data["task_schedules"] = [
        {"command": "reports/morning_summary", "schedules": ["0 9 * * 1-5"]},
        {"command": "bad", "schedules": ["invalid cron"]},
        "not a schedule",
    ]
    save_yaml_file(person_file, person_data)

    snapshot = service.read_person_config(
        config_dir=config_dir,
        person_id="alice",
        env_file_path=env_file,
    )

    assert [schedule.model_dump() for schedule in snapshot.task_schedules] == [
        {"command": "reports/morning_summary", "schedules": ["0 9 * * 1-5"]}
    ]


def test_read_person_config_exposes_non_secret_github_apps_values(
    tmp_path: Path,
) -> None:
    installation_id = 123
    app_id = 456
    config_dir = tmp_path / ".guildbotics/config"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"ALICE_GITHUB_INSTALLATION_ID={installation_id}",
                f"ALICE_GITHUB_APP_ID={app_id}",
                "ALICE_GITHUB_PRIVATE_KEY_PATH=/secure/private-key.pem",
                "ALICE_GITHUB_ACCESS_TOKEN=secret-token",
            ]
        )
    )
    service = SimplePersonSetupService()
    service.write_person(
        PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file,
            append_env_file=False,
            person_type="github_apps",
            person_id="alice",
            person_name="Alice",
            is_active=True,
            github_username="alice[bot]",
            git_email="1+alice[bot]@users.noreply.github.com",
            roles=["architect"],
            speaking_style="style-a",
        )
    )

    snapshot = service.read_person_config(
        config_dir=config_dir,
        person_id="alice",
        env_file_path=env_file,
    )

    assert snapshot.github_installation_id == installation_id
    assert snapshot.github_app_id == app_id
    assert snapshot.github_private_key_path == "/secure/private-key.pem"
    assert snapshot.has_github_access_token is True

    service.update_person(
        PersonUpdateInput(
            config_dir=config_dir,
            env_file_path=env_file,
            original_person_id="alice",
            append_env_file=False,
            person_type="machine_user",
            person_id="alice-renamed",
            person_name="Alice Updated",
            is_active=False,
            github_username="alice-renamed",
            git_email="2+alice-renamed@users.noreply.github.com",
            roles=["reviewer"],
            speaking_style="style-b",
            relationships="rel-b",
            character={
                "archetype": "strategic_pm",
                "conversation_preferences": {
                    "join_when": ["planning"],
                    "avoid_when": ["noise"],
                    "contribution_style": ["risk review"],
                },
            },
            github_access_token="token-b",
            slack_bot_token="xoxb-b",
            slack_app_token="xapp-b",
            slack_channels=["C2"],
        )
    )

    renamed_file = config_dir / "team/members/alice-renamed/person.yml"
    assert renamed_file.exists()
    text = renamed_file.read_text()
    assert "person_id: alice-renamed" in text
    assert "name: Alice Updated" in text
    assert "archetype: strategic_pm" in text
    env_text = env_file.read_text()
    assert "ALICE_GITHUB_ACCESS_TOKEN" not in env_text
    assert "ALICE_RENAMED_GITHUB_ACCESS_TOKEN=token-b" in env_text
    assert "ALICE_RENAMED_SLACK_BOT_TOKEN=xoxb-b" in env_text

    service.delete_person(
        config_dir=config_dir,
        person_id="alice-renamed",
        env_file_path=env_file,
    )
    assert not renamed_file.exists()
    env_text_after_delete = env_file.read_text()
    assert "ALICE_RENAMED_GITHUB_ACCESS_TOKEN" not in env_text_after_delete
    assert "ALICE_RENAMED_SLACK_BOT_TOKEN" not in env_text_after_delete


def test_update_person_preserves_existing_secrets_when_blank(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ALICE_GITHUB_ACCESS_TOKEN=github-token",
                "ALICE_SLACK_BOT_TOKEN=xoxb-token",
                "ALICE_SLACK_APP_TOKEN=xapp-token",
            ]
        )
    )
    service = SimplePersonSetupService()
    service.write_person(
        PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file,
            append_env_file=False,
            person_type="machine_user",
            person_id="alice",
            person_name="Alice",
            is_active=True,
            github_username="alice",
            git_email="1+alice@users.noreply.github.com",
            roles=["architect"],
            speaking_style="style-a",
            relationships="rel-a",
            slack_channels=["general"],
        )
    )

    service.update_person(
        PersonUpdateInput(
            config_dir=config_dir,
            env_file_path=env_file,
            original_person_id="alice",
            append_env_file=False,
            person_type="machine_user",
            person_id="alice-renamed",
            person_name="Alice Updated",
            is_active=True,
            github_username="alice",
            git_email="1+alice@users.noreply.github.com",
            roles=["reviewer"],
            speaking_style="style-b",
            relationships="rel-b",
            slack_channels=["random"],
        )
    )

    env_text = env_file.read_text()
    assert "ALICE_GITHUB_ACCESS_TOKEN" not in env_text
    assert "ALICE_RENAMED_GITHUB_ACCESS_TOKEN=github-token" in env_text
    assert "ALICE_RENAMED_SLACK_BOT_TOKEN=xoxb-token" in env_text
    assert "ALICE_RENAMED_SLACK_APP_TOKEN=xapp-token" in env_text
