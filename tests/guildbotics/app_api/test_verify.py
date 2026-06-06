from pathlib import Path

import pytest

from guildbotics.app_api import verify as verify_module
from guildbotics.app_api.models import ConfigStatus
from guildbotics.app_api.verify import VerifyService
from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.github.github_utils import GitHubAppAuth

PROVIDER_ENV_KEYS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
)


def _config_status(
    tmp_path: Path,
    *,
    env_file_exists: bool = True,
    project_file_exists: bool = True,
) -> ConfigStatus:
    env_file = tmp_path / ".env"
    if env_file_exists:
        env_file.write_text("")
    primary_project_file = tmp_path / ".guildbotics/config/team/project.yml"
    primary_project_file.parent.mkdir(parents=True, exist_ok=True)
    if project_file_exists:
        primary_project_file.write_text("language: en\n")
    home_project_file = tmp_path / "home/.guildbotics/config/team/project.yml"
    return ConfigStatus(
        cwd=tmp_path,
        env_file=env_file,
        env_file_exists=env_file.exists(),
        primary_config_dir=tmp_path / ".guildbotics/config",
        primary_project_file=primary_project_file,
        primary_project_file_exists=primary_project_file.exists(),
        home_config_dir=tmp_path / "home/.guildbotics/config",
        home_project_file=home_project_file,
        home_project_file_exists=home_project_file.exists(),
        storage_dir=tmp_path / "home/.guildbotics/data",
    )


def _isolated_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point config resolution at the workspace and neutralize ambient env."""
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path / ".guildbotics/config"))
    monkeypatch.setenv("PATH", "")
    for key in PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_model_mapping(tmp_path: Path, default: str) -> None:
    mapping = tmp_path / ".guildbotics/config/intelligences/model_mapping.yml"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text(f"default: {default}\n")


def _write_cli_agent(tmp_path: Path, mapping_default: str, script: str) -> None:
    cli_mapping = tmp_path / ".guildbotics/config/intelligences/cli_agent_mapping.yml"
    cli_mapping.parent.mkdir(parents=True, exist_ok=True)
    cli_mapping.write_text(f"default: {mapping_default}\n")
    cli_agent = (
        tmp_path / ".guildbotics/config/intelligences/cli_agents" / mapping_default
    )
    cli_agent.parent.mkdir(parents=True, exist_ok=True)
    cli_agent.write_text(f"script: {script}\n")


def _checks_by_code(response) -> dict:
    return {check.code: check for check in response.checks}


def test_verify_reports_missing_active_member(tmp_path: Path) -> None:
    config = _config_status(tmp_path)
    team = Team(project=Project(name="demo"), members=[])

    response = VerifyService().verify(config=config, team=team)

    assert not response.ok
    assert response.active_members == []
    assert "active_members" in {check.code for check in response.errors}


def test_verify_reports_missing_project_file(tmp_path: Path) -> None:
    config = _config_status(tmp_path, project_file_exists=False)
    team = Team(project=Project(name="demo"), members=[])

    response = VerifyService().verify(config=config, team=team)

    checks = _checks_by_code(response)
    assert checks["config_project_file"].status == "error"
    assert not response.ok


def test_verify_reports_missing_env_file_as_warning(tmp_path: Path) -> None:
    config = _config_status(tmp_path, env_file_exists=False)
    team = Team(
        project=Project(name="demo"),
        members=[Person(person_id="alice", name="Alice", is_active=True)],
    )

    response = VerifyService().verify(config=config, team=team)

    checks = _checks_by_code(response)
    assert checks["env_file"].status == "warning"
    assert "env_file" in {check.code for check in response.warnings}


def test_verify_team_load_error(tmp_path: Path) -> None:
    config = _config_status(tmp_path)

    response = VerifyService().verify(
        config=config, team=None, team_error=ValueError("broken team")
    )

    checks = _checks_by_code(response)
    assert checks["team_load"].status == "error"
    assert checks["team_load"].message == "broken team"
    assert checks["team_load"].context == {"error_type": "ValueError"}
    assert not response.ok


def test_verify_team_load_none_without_error(tmp_path: Path) -> None:
    config = _config_status(tmp_path)

    response = VerifyService().verify(config=config, team=None)

    checks = _checks_by_code(response)
    assert checks["team_load"].status == "error"
    assert checks["team_load"].message == "Team config could not be loaded."
    # LLM / cli / github checks are skipped when there is no team.
    assert "llm_api_key" not in checks


def test_verify_multiple_active_members(tmp_path: Path) -> None:
    config = _config_status(tmp_path)
    team = Team(
        project=Project(name="demo"),
        members=[
            Person(person_id="alice", name="Alice", is_active=True),
            Person(person_id="bob", name="Bob", is_active=True),
            Person(person_id="carol", name="Carol", is_active=False),
        ],
    )

    response = VerifyService().verify(config=config, team=team)

    checks = _checks_by_code(response)
    assert response.active_members == ["alice", "bob"]
    assert checks["active_members"].status == "ok"
    assert checks["active_members"].context["active_members"] == ["alice", "bob"]


@pytest.mark.parametrize(
    ("default_model", "expected_key"),
    [
        ("models/openai/gpt-5-mini.yml", "OPENAI_API_KEY"),
        ("models/gemini/gemini-2.5-pro.yml", "GOOGLE_API_KEY"),
        ("models/google/gemini-2.5-pro.yml", "GOOGLE_API_KEY"),
        ("models/anthropic/claude.yml", "ANTHROPIC_API_KEY"),
        ("models/claude/claude.yml", "ANTHROPIC_API_KEY"),
    ],
)
def test_verify_llm_provider_api_key_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    default_model: str,
    expected_key: str,
) -> None:
    _isolated_config_env(tmp_path, monkeypatch)
    config = _config_status(tmp_path)
    config.env_file.write_text(f"{expected_key}=secret\n")
    _write_model_mapping(tmp_path, default_model)
    team = Team(
        project=Project(name="demo"),
        members=[Person(person_id="alice", name="Alice", is_active=True)],
    )

    response = VerifyService().verify(config=config, team=team)

    check = _checks_by_code(response)["llm_api_key"]
    assert check.status == "ok"
    assert check.target == expected_key


def test_verify_llm_provider_api_key_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_config_env(tmp_path, monkeypatch)
    config = _config_status(tmp_path)
    _write_model_mapping(tmp_path, "models/openai/gpt-5-mini.yml")
    team = Team(
        project=Project(name="demo"),
        members=[Person(person_id="alice", name="Alice", is_active=True)],
    )

    response = VerifyService().verify(config=config, team=team)

    check = _checks_by_code(response)["llm_api_key"]
    assert check.status == "error"
    assert check.target == "OPENAI_API_KEY"
    assert check in response.errors


def test_verify_llm_provider_unknown_is_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_config_env(tmp_path, monkeypatch)
    config = _config_status(tmp_path)
    _write_model_mapping(tmp_path, "models/unknown/whatever.yml")
    team = Team(
        project=Project(name="demo"),
        members=[Person(person_id="alice", name="Alice", is_active=True)],
    )

    response = VerifyService().verify(config=config, team=team)

    check = _checks_by_code(response)["llm_provider"]
    assert check.status == "warning"
    assert check.context == {"provider": ""}


def test_verify_cli_agent_executable_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_config_env(tmp_path, monkeypatch)
    config = _config_status(tmp_path)
    _write_model_mapping(tmp_path, "models/openai/gpt-5-mini.yml")
    _write_cli_agent(tmp_path, "codex-cli.yml", "codex exec")
    config.env_file.write_text("PATH=/custom/bin\n")

    resolved: dict[str, object] = {}

    def _resolve(executable: str, search_path: str | None) -> str:
        resolved["executable"] = executable
        resolved["search_path"] = search_path
        return "/custom/bin/codex"

    monkeypatch.setattr(verify_module, "resolve_cli_agent_path", _resolve)

    team = Team(
        project=Project(name="demo"),
        members=[Person(person_id="alice", name="Alice", is_active=True)],
    )

    response = VerifyService().verify(config=config, team=team)

    check = _checks_by_code(response)["cli_agent_executable"]
    assert check.status == "ok"
    assert check.target == "codex"
    assert check.context["path"] == "/custom/bin/codex"
    # The .env PATH value is passed through to the resolver.
    assert resolved == {"executable": "codex", "search_path": "/custom/bin"}


def test_verify_cli_agent_executable_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_config_env(tmp_path, monkeypatch)
    config = _config_status(tmp_path)
    _write_model_mapping(tmp_path, "models/openai/gpt-5-mini.yml")
    _write_cli_agent(tmp_path, "codex-cli.yml", "codex exec")

    monkeypatch.setattr(verify_module, "resolve_cli_agent_path", lambda *_a, **_k: "")

    team = Team(
        project=Project(name="demo"),
        members=[Person(person_id="alice", name="Alice", is_active=True)],
    )

    response = VerifyService().verify(config=config, team=team)

    check = _checks_by_code(response)["cli_agent_executable"]
    assert check.status == "error"
    assert check.target == "codex"
    assert check in response.errors


def test_verify_cli_agent_mapping_missing_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_config_env(tmp_path, monkeypatch)
    config = _config_status(tmp_path)
    _write_model_mapping(tmp_path, "models/openai/gpt-5-mini.yml")
    # Mapping points to a definition whose script names no known executable.
    _write_cli_agent(tmp_path, "ghost-cli.yml", "python run.py")

    team = Team(
        project=Project(name="demo"),
        members=[Person(person_id="alice", name="Alice", is_active=True)],
    )

    response = VerifyService().verify(config=config, team=team)

    checks = _checks_by_code(response)
    assert "cli_agent_executable" not in checks
    assert checks["cli_agent_mapping"].status == "warning"


def test_verify_github_disabled_skips_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_config_env(tmp_path, monkeypatch)
    config = _config_status(tmp_path)
    _write_model_mapping(tmp_path, "models/openai/gpt-5-mini.yml")
    _write_cli_agent(tmp_path, "codex-cli.yml", "codex exec")
    team = Team(
        project=Project(name="demo"),
        members=[
            Person(
                person_id="alice",
                name="Alice",
                is_active=True,
                person_type=GitHubAppAuth.MACHINE_USER,
            )
        ],
    )

    response = VerifyService().verify(config=config, team=team)

    assert "github_credential" not in _checks_by_code(response)


@pytest.mark.parametrize(
    ("person_type", "env_keys"),
    [
        (
            GitHubAppAuth.GITHUB_APPS,
            (
                "ALICE_GITHUB_INSTALLATION_ID",
                "ALICE_GITHUB_APP_ID",
                "ALICE_GITHUB_PRIVATE_KEY_PATH",
            ),
        ),
        (GitHubAppAuth.MACHINE_USER, ("ALICE_GITHUB_ACCESS_TOKEN",)),
        (GitHubAppAuth.PROXY_AGENT, ("ALICE_GITHUB_ACCESS_TOKEN",)),
    ],
)
def test_verify_github_credentials_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    person_type: str,
    env_keys: tuple[str, ...],
) -> None:
    _isolated_config_env(tmp_path, monkeypatch)
    config = _config_status(tmp_path)
    config.env_file.write_text("\n".join(f"{key}=value" for key in env_keys) + "\n")
    _write_model_mapping(tmp_path, "models/openai/gpt-5-mini.yml")
    _write_cli_agent(tmp_path, "codex-cli.yml", "codex exec")
    team = Team(
        project=Project(name="demo", services={"ticket_manager": {"name": "GitHub"}}),
        members=[
            Person(
                person_id="alice",
                name="Alice",
                is_active=True,
                person_type=person_type,
            )
        ],
    )

    response = VerifyService().verify(config=config, team=team)

    github_checks = [
        check for check in response.checks if check.code == "github_credential"
    ]
    assert len(github_checks) == len(env_keys)
    assert all(check.status == "ok" for check in github_checks)
    assert {check.target for check in github_checks} == set(env_keys)


@pytest.mark.parametrize(
    ("person_type", "env_key"),
    [
        (GitHubAppAuth.GITHUB_APPS, "ALICE_GITHUB_APP_ID"),
        (GitHubAppAuth.MACHINE_USER, "ALICE_GITHUB_ACCESS_TOKEN"),
        (GitHubAppAuth.PROXY_AGENT, "ALICE_GITHUB_ACCESS_TOKEN"),
    ],
)
def test_verify_github_credentials_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    person_type: str,
    env_key: str,
) -> None:
    _isolated_config_env(tmp_path, monkeypatch)
    config = _config_status(tmp_path)
    _write_model_mapping(tmp_path, "models/openai/gpt-5-mini.yml")
    _write_cli_agent(tmp_path, "codex-cli.yml", "codex exec")
    team = Team(
        project=Project(name="demo", services={"ticket_manager": {"name": "GitHub"}}),
        members=[
            Person(
                person_id="alice",
                name="Alice",
                is_active=True,
                person_type=person_type,
            )
        ],
    )

    response = VerifyService().verify(config=config, team=team)

    missing = {
        check.target for check in response.errors if check.code == "github_credential"
    }
    assert env_key in missing
    assert not response.ok


def test_verify_human_member_has_no_github_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_config_env(tmp_path, monkeypatch)
    config = _config_status(tmp_path)
    _write_model_mapping(tmp_path, "models/openai/gpt-5-mini.yml")
    _write_cli_agent(tmp_path, "codex-cli.yml", "codex exec")
    team = Team(
        project=Project(name="demo", services={"ticket_manager": {"name": "GitHub"}}),
        members=[
            Person(
                person_id="alice",
                name="Alice",
                is_active=True,
                person_type=GitHubAppAuth.HUMAN,
            )
        ],
    )

    response = VerifyService().verify(config=config, team=team)

    assert "github_credential" not in _checks_by_code(response)


def test_verify_checks_env_keys_and_github_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path / ".guildbotics/config"))
    monkeypatch.setenv("PATH", "")
    config = _config_status(tmp_path)
    config.env_file.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=openai-key",
                "ALICE_GITHUB_ACCESS_TOKEN=github-token",
            ]
        )
    )
    project = Project(
        name="demo",
        services={"ticket_manager": {"name": "GitHub"}},
    )
    team = Team(
        project=project,
        members=[
            Person(
                person_id="alice",
                name="Alice",
                is_active=True,
                person_type=GitHubAppAuth.MACHINE_USER,
            )
        ],
    )
    model_mapping = tmp_path / ".guildbotics/config/intelligences/model_mapping.yml"
    model_mapping.parent.mkdir(parents=True, exist_ok=True)
    model_mapping.write_text("default: models/openai/gpt-5-mini.yml\n")
    cli_mapping = tmp_path / ".guildbotics/config/intelligences/cli_agent_mapping.yml"
    cli_mapping.write_text("default: codex-cli.yml\n")
    cli_agent = tmp_path / ".guildbotics/config/intelligences/cli_agents/codex-cli.yml"
    cli_agent.parent.mkdir(parents=True, exist_ok=True)
    cli_agent.write_text("script: codex exec\n")

    response = VerifyService().verify(config=config, team=team)

    checks = {check.code: check for check in response.checks}
    assert response.ok is False
    assert checks["llm_api_key"].status == "ok"
    assert checks["github_credential"].status == "ok"
    assert checks["cli_agent_executable"].status == "error"
    assert response.errors == [checks["cli_agent_executable"]]
