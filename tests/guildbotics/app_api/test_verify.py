from pathlib import Path

from guildbotics.app_api.models import ConfigStatus
from guildbotics.app_api.verify import VerifyService
from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.github.github_utils import GitHubAppAuth


def _config_status(tmp_path: Path, *, env_file_exists: bool = True) -> ConfigStatus:
    env_file = tmp_path / ".env"
    if env_file_exists:
        env_file.write_text("")
    primary_project_file = tmp_path / ".guildbotics/config/team/project.yml"
    primary_project_file.parent.mkdir(parents=True, exist_ok=True)
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


def test_verify_reports_missing_active_member(tmp_path: Path) -> None:
    config = _config_status(tmp_path)
    team = Team(project=Project(name="demo"), members=[])

    response = VerifyService().verify(config=config, team=team)

    assert not response.ok
    assert response.active_members == []
    assert "active_members" in {check.code for check in response.errors}


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
