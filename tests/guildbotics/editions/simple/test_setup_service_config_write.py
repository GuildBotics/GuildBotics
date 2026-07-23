"""Config-write coverage for the simple setup service.

These tests extend ``test_setup_service.py`` and focus on the file/env/YAML
write behaviours that the existing suite did not yet exercise:

- project init file set across workspace / home / custom config locations
- ``.env`` skip / append / overwrite operations for ``write_project``
- secret blank-update must not clear an existing secret (same-id update)
- GitHub disabled -> enabled -> disabled project diff
- repo access https / ssh round trip
- member add / update / delete env-key add / update / remove
- member-id rename moves directory + env keys + slack channel mapping
- ``task_schedules`` / ``routine_commands`` YAML round trip
- character / relationships / speaking_style round trip

All filesystem writes use ``tmp_path`` so the real home directory is never
touched.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.editions.simple.setup_service import (
    LaneMapInput,
    PersonSetupInput,
    PersonUpdateInput,
    ProjectSetupInput,
    ProjectUpdateInput,
    SimplePersonSetupService,
    SimpleProjectSetupService,
)
from guildbotics.utils.fileio import (
    get_template_path,
    load_markdown_with_frontmatter,
    load_yaml_file,
)
from guildbotics.utils.text_utils import replace_placeholders

# Relative config-dir layout produced by ``write_project`` independent of the
# absolute config location (workspace / home / custom).
PROJECT_RELATIVE_FILES = {
    "team/project.yml",
    "intelligences/model_mapping.yml",
    "intelligences/cli_agent_mapping.yml",
    "intelligences/native_agent_policy.yml",
    "commands/translate.md",
    "commands/summarize.md",
    "commands/context-info.md",
    "commands/get-time-of-day.yml",
}


def _project_input(
    config_dir: Path, env_file_path: Path, **overrides
) -> ProjectSetupInput:
    payload: dict = {
        "config_dir": config_dir,
        "env_file_path": env_file_path,
        "env_file_option": "overwrite",
        "language": "en",
        "llm_api_type": "openai",
        "cli_agent": "codex",
        "provider_api_keys": {"openai": "test-openai-key"},
    }
    payload.update(overrides)
    return ProjectSetupInput(**payload)


def _github_project_input(
    config_dir: Path, env_file_path: Path, **overrides
) -> ProjectSetupInput:
    return _project_input(
        config_dir,
        env_file_path,
        owner="GuildBotics",
        project_id="1",
        github_project_url="https://github.com/orgs/GuildBotics/projects/1",
        **overrides,
    )


def _person_input(
    config_dir: Path, env_file_path: Path, **overrides
) -> PersonSetupInput:
    payload: dict = {
        "config_dir": config_dir,
        "env_file_path": env_file_path,
        "append_env_file": False,
        "person_type": "machine_user",
        "person_id": "alice",
        "person_name": "Alice",
        "is_active": True,
        "github_username": "alice",
        "git_email": "1+alice@users.noreply.github.com",
        "roles": ["architect"],
        "speaking_style": "style-a",
    }
    payload.update(overrides)
    return PersonSetupInput(**payload)


def _env_dict(env_file_path: Path) -> dict[str, str]:
    text = env_file_path.read_text()
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


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
    env_file_path = tmp_path / location / ".env"

    result = SimpleProjectSetupService().write_project(
        _github_project_input(config_dir, env_file_path)
    )

    created_paths = {created_file.path for created_file in result.files}
    for relative in PROJECT_RELATIVE_FILES:
        expected = config_dir / relative
        assert expected in created_paths
        assert expected.exists()
    # The cli_agent config bundle is copied alongside the mappings.
    cli_agent_files = {
        path for path in created_paths if "intelligences/cli_agents/" in str(path)
    }
    assert cli_agent_files
    for path in cli_agent_files:
        assert path.exists()
    # The env file lives wherever the caller pointed it, even outside config_dir.
    assert env_file_path in created_paths
    assert env_file_path.exists()


@pytest.mark.parametrize("language", ["en", "ja"])
def test_sample_command_brains_exist_in_template_mapping(language: str) -> None:
    sample_dir = (
        Path("guildbotics/editions/simple/templates/sample_commands") / language
    )
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
        Path("guildbotics/editions/simple/templates/sample_commands")
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
# .env skip / append / overwrite
# --------------------------------------------------------------------------- #


def test_write_project_env_skip_leaves_existing_file_untouched(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"
    env_file_path.write_text("PRESERVED=1")

    result = SimpleProjectSetupService().write_project(
        _github_project_input(config_dir, env_file_path, env_file_option="skip")
    )

    created_paths = {created_file.path for created_file in result.files}
    assert env_file_path not in created_paths
    assert env_file_path.read_text() == "PRESERVED=1"


def test_write_project_env_overwrite_replaces_file(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"
    env_file_path.write_text("OLD=value\nOPENAI_API_KEY=stale")

    result = SimpleProjectSetupService().write_project(
        _github_project_input(config_dir, env_file_path, env_file_option="overwrite")
    )

    actions = {created_file.path: created_file.action for created_file in result.files}
    assert actions[env_file_path] == "create"
    text = env_file_path.read_text()
    assert "OLD=value" not in text
    assert "OPENAI_API_KEY=test-openai-key" in text


def test_write_project_env_append_keeps_existing_lines(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"
    env_file_path.write_text("EXISTING=keep")

    result = SimpleProjectSetupService().write_project(
        _github_project_input(config_dir, env_file_path, env_file_option="append")
    )

    actions = {created_file.path: created_file.action for created_file in result.files}
    assert actions[env_file_path] == "append"
    text = env_file_path.read_text()
    assert text.startswith("EXISTING=keep")
    assert "OPENAI_API_KEY=test-openai-key" in text


def test_project_input_append_requires_existing_env_file(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"

    with pytest.raises(ValueError, match="append requires an existing env file"):
        _github_project_input(config_dir, env_file_path, env_file_option="append")


def test_write_project_empty_intelligence_selection_keeps_template_defaults(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"

    SimpleProjectSetupService().write_project(
        _project_input(
            config_dir,
            env_file_path,
            llm_api_type="",
            cli_agent="",
            provider_api_keys={},
        )
    )

    model_mapping = load_yaml_file(config_dir / "intelligences/model_mapping.yml")
    cli_mapping = load_yaml_file(config_dir / "intelligences/cli_agent_mapping.yml")
    assert model_mapping["default"] == "models/openai/default.yml"
    assert cli_mapping["default"] == "codex"


def test_write_project_creates_policy_from_template_without_overwriting(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"
    service = SimpleProjectSetupService()

    service.write_project(_project_input(config_dir, env_file_path))
    policy_file = config_dir / "intelligences/native_agent_policy.yml"
    template_file = get_template_path() / "intelligences/native_agent_policy.yml"
    assert policy_file.read_text(encoding="utf-8") == template_file.read_text(
        encoding="utf-8"
    )

    policy_file.write_text("codex:\n  filesystem_access: host\n", encoding="utf-8")
    result = service.write_project(_project_input(config_dir, env_file_path))

    assert policy_file.read_text(encoding="utf-8") == (
        "codex:\n  filesystem_access: host\n"
    )
    assert policy_file not in {created.path for created in result.files}


# --------------------------------------------------------------------------- #
# GitHub disabled -> enabled -> disabled diff + repo access https / ssh
# --------------------------------------------------------------------------- #


def _seed_project(config_dir: Path, env_file_path: Path) -> None:
    SimpleProjectSetupService().write_project(_project_input(config_dir, env_file_path))


def test_update_project_github_disabled_enabled_disabled_diff(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"
    _seed_project(config_dir, env_file_path)
    service = SimpleProjectSetupService()
    project_file = config_dir / "team/project.yml"

    base = {
        "config_dir": config_dir,
        "env_file_path": env_file_path,
        "language": "en",
        "llm_api_type": "openai",
        "cli_agent": "codex",
    }

    # Initially disabled: no GitHub services present.
    service.update_project(ProjectUpdateInput(**base, github_enabled=False))
    disabled = load_yaml_file(project_file)
    assert "services" not in disabled
    assert "repositories" not in disabled

    # Enabled: ticket_manager + code_hosting_service added (no repository entry;
    # the repository is derived from each issue at runtime).
    service.update_project(
        ProjectUpdateInput(
            **base,
            github_enabled=True,
            owner="GuildBotics",
            project_id="42",
            github_project_url="https://github.com/orgs/GuildBotics/projects/42",
        )
    )
    enabled = load_yaml_file(project_file)
    services = enabled["services"]
    assert services["ticket_manager"]["owner"] == "GuildBotics"
    assert services["ticket_manager"]["project_id"] == "42"
    assert services["ticket_manager"]["url"].endswith("/projects/42")
    assert services["code_hosting_service"]["owner"] == "GuildBotics"
    assert "repositories" not in enabled

    # Disabled again: GitHub services removed.
    service.update_project(ProjectUpdateInput(**base, github_enabled=False))
    re_disabled = load_yaml_file(project_file)
    assert "services" not in re_disabled
    assert "repositories" not in re_disabled


def test_update_project_enables_github_from_project_url(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"
    _seed_project(config_dir, env_file_path)
    service = SimpleProjectSetupService()

    service.update_project(
        ProjectUpdateInput(
            config_dir=config_dir,
            env_file_path=env_file_path,
            language="en",
            llm_api_type="openai",
            cli_agent="codex",
            github_enabled=True,
            owner="GuildBotics",
            project_id="1",
            github_project_url="https://github.com/orgs/GuildBotics/projects/1",
        )
    )

    stored = load_yaml_file(config_dir / "team/project.yml")
    code_hosting = stored["services"]["code_hosting_service"]
    assert code_hosting["owner"] == "GuildBotics"
    # Clone access is always HTTPS now, so no repo_base_url is persisted.
    assert "repo_base_url" not in code_hosting
    snapshot = service.read_project_config(
        config_dir=config_dir, env_file_path=env_file_path
    )
    assert snapshot.github_enabled is True


def test_update_project_empty_intelligence_selection_preserves_existing_defaults(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"
    SimpleProjectSetupService().write_project(
        _project_input(
            config_dir,
            env_file_path,
            llm_api_type="gemini",
            cli_agent="claude",
            provider_api_keys={},
        )
    )

    SimpleProjectSetupService().update_project(
        ProjectUpdateInput(
            config_dir=config_dir,
            env_file_path=env_file_path,
            language="en",
            llm_api_type="",
            cli_agent="",
            github_enabled=False,
        )
    )

    model_mapping = load_yaml_file(config_dir / "intelligences/model_mapping.yml")
    cli_mapping = load_yaml_file(config_dir / "intelligences/cli_agent_mapping.yml")
    assert model_mapping["default"] == "models/gemini/default.yml"
    assert cli_mapping["default"] == "claude"


# --------------------------------------------------------------------------- #
# lane_map: persist, defaults, custom values, round-trip, github-disabled
# --------------------------------------------------------------------------- #


def test_write_project_persists_default_lane_map(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"
    SimpleProjectSetupService().write_project(
        _github_project_input(config_dir, env_file_path)
    )

    stored = load_yaml_file(config_dir / "team/project.yml")
    assert stored["services"]["ticket_manager"]["lane_map"] == {
        "ready": "Todo",
        "working": "In Progress",
        "done": "Done",
    }


def test_write_project_persists_custom_lane_map(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"
    SimpleProjectSetupService().write_project(
        _github_project_input(
            config_dir,
            env_file_path,
            lane_map=LaneMapInput(ready="Ready", working="Doing", done="Shipped"),
        )
    )

    stored = load_yaml_file(config_dir / "team/project.yml")
    assert stored["services"]["ticket_manager"]["lane_map"] == {
        "ready": "Ready",
        "working": "Doing",
        "done": "Shipped",
    }


def test_blank_working_lane_falls_back_to_default(tmp_path: Path) -> None:
    # The YAML layer strips empty strings, so a blank working lane cannot be
    # persisted; it falls back to the default "In Progress".
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"
    SimpleProjectSetupService().write_project(
        _github_project_input(
            config_dir,
            env_file_path,
            lane_map=LaneMapInput(ready="Todo", working="", done="Done"),
        )
    )

    stored = load_yaml_file(config_dir / "team/project.yml")
    assert stored["services"]["ticket_manager"]["lane_map"]["working"] == "In Progress"


def test_lane_map_rejects_identical_ready_and_done() -> None:
    with pytest.raises(ValueError):
        LaneMapInput(ready="Same", done="Same")


def test_read_project_config_round_trips_lane_map(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"
    service = SimpleProjectSetupService()
    service.write_project(
        _github_project_input(
            config_dir,
            env_file_path,
            lane_map=LaneMapInput(ready="Ready", working="Doing", done="Shipped"),
        )
    )

    snapshot = service.read_project_config(
        config_dir=config_dir, env_file_path=env_file_path
    )
    assert snapshot.lane_map.ready == "Ready"
    assert snapshot.lane_map.working == "Doing"
    assert snapshot.lane_map.done == "Shipped"


def test_update_project_github_disabled_writes_no_lane_map(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    env_file_path = tmp_path / ".env"
    _seed_project(config_dir, env_file_path)
    SimpleProjectSetupService().update_project(
        ProjectUpdateInput(
            config_dir=config_dir,
            env_file_path=env_file_path,
            language="en",
            llm_api_type="openai",
            cli_agent="codex",
            github_enabled=False,
        )
    )

    stored = load_yaml_file(config_dir / "team/project.yml")
    assert "services" not in stored


# --------------------------------------------------------------------------- #
# secret blank-update must not clear an existing secret (same-id update)
# --------------------------------------------------------------------------- #


def test_update_person_blank_secret_keeps_existing_value_same_id(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    env_file = tmp_path / ".env"
    env_file.write_text("GLOBAL=keep")
    service = SimplePersonSetupService()

    service.write_person(
        _person_input(
            config_dir,
            env_file,
            append_env_file=True,
            github_access_token="github-token",
            slack_bot_token="xoxb-token",
        )
    )
    assert _env_dict(env_file)["ALICE_GITHUB_ACCESS_TOKEN"] == "github-token"

    # Update with blank secrets and an unrelated profile change.
    service.update_person(
        PersonUpdateInput(
            config_dir=config_dir,
            env_file_path=env_file,
            original_person_id="alice",
            append_env_file=False,
            person_type="machine_user",
            person_id="alice",
            person_name="Alice Updated",
            is_active=True,
            github_username="alice",
            git_email="1+alice@users.noreply.github.com",
            roles=["reviewer"],
            speaking_style="style-b",
            github_access_token="",
            slack_bot_token="",
        )
    )

    env_values = _env_dict(env_file)
    assert env_values["ALICE_GITHUB_ACCESS_TOKEN"] == "github-token"
    assert env_values["ALICE_SLACK_BOT_TOKEN"] == "xoxb-token"


# --------------------------------------------------------------------------- #
# member add / update / delete: env secret key added / updated / removed
# --------------------------------------------------------------------------- #


def test_member_crud_adds_updates_and_removes_env_keys(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    env_file = tmp_path / ".env"
    env_file.write_text("GLOBAL=keep")
    service = SimplePersonSetupService()

    # add (append): env key created.
    service.write_person(
        _person_input(
            config_dir,
            env_file,
            append_env_file=True,
            github_access_token="token-v1",
        )
    )
    assert _env_dict(env_file)["ALICE_GITHUB_ACCESS_TOKEN"] == "token-v1"

    # update (same id): env key value replaced.
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
            github_access_token="token-v2",
        )
    )
    env_values = _env_dict(env_file)
    assert env_values["ALICE_GITHUB_ACCESS_TOKEN"] == "token-v2"
    assert env_values["GLOBAL"] == "keep"

    # delete: env key removed, unrelated key preserved.
    service.delete_person(
        config_dir=config_dir, person_id="alice", env_file_path=env_file
    )
    env_values = _env_dict(env_file)
    assert "ALICE_GITHUB_ACCESS_TOKEN" not in env_values
    assert env_values["GLOBAL"] == "keep"
    assert not (config_dir / "team/members/alice/person.yml").exists()


# --------------------------------------------------------------------------- #
# member id rename: directory / env key / slack mapping are moved
# --------------------------------------------------------------------------- #


def test_member_rename_moves_directory_env_keys_and_slack_mapping(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    env_file = tmp_path / ".env"
    env_file.write_text("GLOBAL=keep")
    service = SimplePersonSetupService()

    service.write_person(
        _person_input(
            config_dir,
            env_file,
            append_env_file=True,
            person_id="old-id",
            github_username="old-id",
            github_access_token="token-keep",
            slack_bot_token="xoxb-keep",
            slack_channels=["general"],
        )
    )
    old_dir = config_dir / "team/members/old-id"
    assert old_dir.exists()
    old_person = load_yaml_file(old_dir / "person.yml")
    assert old_person["message_channels"][0]["used_by"] == ["old-id"]

    service.update_person(
        PersonUpdateInput(
            config_dir=config_dir,
            env_file_path=env_file,
            original_person_id="old-id",
            append_env_file=False,
            person_type="machine_user",
            person_id="new-id",
            person_name="Alice",
            is_active=True,
            github_username="new-id",
            git_email="1+new-id@users.noreply.github.com",
            roles=["architect"],
            speaking_style="style-a",
            # Blank secrets so we verify the rename PRESERVES existing values.
            slack_channels=["general"],
        )
    )

    new_dir = config_dir / "team/members/new-id"
    assert new_dir.exists()
    assert not old_dir.exists()

    new_person = load_yaml_file(new_dir / "person.yml")
    assert new_person["person_id"] == "new-id"
    # Slack channel ownership mapping follows the new id.
    assert new_person["message_channels"][0]["used_by"] == ["new-id"]

    env_values = _env_dict(env_file)
    assert "OLD_ID_GITHUB_ACCESS_TOKEN" not in env_values
    assert "OLD_ID_SLACK_BOT_TOKEN" not in env_values
    assert env_values["NEW_ID_GITHUB_ACCESS_TOKEN"] == "token-keep"
    assert env_values["NEW_ID_SLACK_BOT_TOKEN"] == "xoxb-keep"


def test_member_rename_into_existing_id_is_rejected(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    env_file = tmp_path / ".env"
    service = SimplePersonSetupService()

    service.write_person(_person_input(config_dir, env_file, person_id="alice"))
    service.write_person(
        _person_input(
            config_dir,
            env_file,
            person_id="bob",
            github_username="bob",
            git_email="2+bob@users.noreply.github.com",
        )
    )

    with pytest.raises(Exception) as exc_info:
        service.update_person(
            PersonUpdateInput(
                config_dir=config_dir,
                env_file_path=env_file,
                original_person_id="alice",
                append_env_file=False,
                person_type="machine_user",
                person_id="bob",
                person_name="Alice",
                is_active=True,
                github_username="alice",
                git_email="1+alice@users.noreply.github.com",
                roles=["architect"],
                speaking_style="style-a",
            )
        )
    assert getattr(exc_info.value, "code", "") == "person_id_conflict"
    # Both members remain intact.
    assert (config_dir / "team/members/alice/person.yml").exists()
    assert (config_dir / "team/members/bob/person.yml").exists()


# --------------------------------------------------------------------------- #
# task_schedules / routine_commands YAML round trip
# --------------------------------------------------------------------------- #


def test_person_routines_and_schedules_yaml_round_trip(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    env_file = tmp_path / ".env"
    service = SimplePersonSetupService()

    routine_commands = [
        "workflows/ticket_driven_workflow",
        "reports/daily_digest",
    ]
    task_schedules = [
        {"command": "reports/morning region=jp", "schedules": ["0 9 * * 1-5"]},
        {"command": "reports/evening", "schedules": ["0 18 * * *", "30 12 * * 6"]},
    ]

    service.write_person(
        _person_input(
            config_dir,
            env_file,
            routine_commands=routine_commands,
            task_schedules=task_schedules,
        )
    )

    # Raw YAML round trip.
    stored = load_yaml_file(config_dir / "team/members/alice/person.yml")
    assert stored["routine_commands"] == routine_commands
    assert stored["task_schedules"] == task_schedules

    # Snapshot round trip.
    snapshot = service.read_person_config(
        config_dir=config_dir, person_id="alice", env_file_path=env_file
    )
    assert snapshot.routine_commands == routine_commands
    assert [s.model_dump() for s in snapshot.task_schedules] == task_schedules


# --------------------------------------------------------------------------- #
# character / relationships / speaking_style round trip
# --------------------------------------------------------------------------- #


def test_person_character_relationships_speaking_style_round_trip(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    env_file = tmp_path / ".env"
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
    service.write_person(
        _person_input(
            config_dir,
            env_file,
            speaking_style="Concise and direct.",
            relationships="Reports to {pm}; mentors juniors.",
            character=character,
        )
    )

    stored = load_yaml_file(config_dir / "team/members/alice/person.yml")
    assert stored["speaking_style"] == "Concise and direct."
    assert stored["relationships"] == "Reports to {pm}; mentors juniors."
    assert stored["profile"]["character"] == character

    snapshot = service.read_person_config(
        config_dir=config_dir, person_id="alice", env_file_path=env_file
    )
    assert snapshot.speaking_style == "Concise and direct."
    assert snapshot.relationships == "Reports to {pm}; mentors juniors."
    assert snapshot.character == character
