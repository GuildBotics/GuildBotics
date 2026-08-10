import os
from pathlib import Path

import pytest

from guildbotics.intelligences import cli_agents
from guildbotics.intelligences.cli_agents import (
    CLI_AGENTS,
    cli_agent_default_path,
    cli_agent_executable,
    cli_agent_name_from_path,
    get_cli_agent_search_path,
    require_cli_agent_path,
    resolve_cli_agent_path,
    resolve_default_cli_executable,
)


def test_cli_agent_search_path_preserves_explicit_empty_path() -> None:
    assert get_cli_agent_search_path("") == ""


def test_a_tool_is_identified_by_its_definition_directory() -> None:
    """One tool, one spelling -- the same rule a model path follows."""
    assert cli_agent_name_from_path("cli_agents/codex/default.yml") == "codex"
    assert cli_agent_name_from_path("cli_agents/codex/reviewer.yml") == "codex"
    assert cli_agent_name_from_path("cli_agents/copilot/default.yml") == "copilot"
    # Anything that is not a definition path names no tool.
    assert cli_agent_name_from_path("codex") == ""
    assert cli_agent_name_from_path("cli_agents/codex.yml") == ""


def test_a_tools_default_definition_path_is_derived_from_its_name() -> None:
    assert cli_agent_default_path("codex") == "cli_agents/codex/default.yml"


def test_a_catalog_tools_definition_path_is_accepted() -> None:
    assert require_cli_agent_path("cli_agents/codex/default.yml", where="x") == "codex"
    assert require_cli_agent_path("cli_agents/codex/reviewer.yml", where="x") == "codex"


@pytest.mark.parametrize(
    "path",
    [
        # The catalog is closed: no adapter, no run.
        "cli_agents/mytool/default.yml",
        # A path that names no tool at all is just as unrunnable.
        "codex",
        "cli_agents/codex.yml",
    ],
)
def test_a_path_outside_the_catalog_is_rejected_with_the_entry_named(
    path: str,
) -> None:
    with pytest.raises(ValueError) as excinfo:
        require_cli_agent_path(path, where="AI CLI tool slot 'default'")

    message = str(excinfo.value)
    assert "AI CLI tool slot 'default'" in message
    assert path in message
    # The message teaches the fix: it lists every tool that can run.
    assert "codex, claude, grok, copilot, antigravity" in message


def test_every_catalog_tool_ships_a_definition_template() -> None:
    """A tool the editor offers must have a file its effort mapping lands in."""
    template_dir = (
        Path(__file__).parents[3] / "guildbotics/templates/intelligences/cli_agents"
    )
    shipped = {default.parent.name for default in template_dir.glob("*/default.yml")}

    assert shipped == {agent.name for agent in CLI_AGENTS}


def test_the_catalog_is_ordered_and_names_each_tools_binary() -> None:
    agents = {agent.name: agent for agent in CLI_AGENTS}

    assert agents["codex"].executable == "codex"
    assert agents["codex"].config_reference == "cli_agents/codex/default.yml"
    assert agents["grok"].label == "Grok Build"
    # Antigravity is the one tool whose binary is not named after it.
    assert agents["antigravity"].executable == "agy"
    assert [agent.order for agent in CLI_AGENTS] == sorted(
        agent.order for agent in CLI_AGENTS
    )


def test_cli_agent_search_path_falls_back_for_ambient_empty_path(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")

    path = get_cli_agent_search_path()

    entries = path.split(os.pathsep)
    assert "/usr/bin" in entries
    assert "/opt/homebrew/bin" in entries


def test_cli_agent_search_path_adds_gui_app_fallbacks() -> None:
    path = get_cli_agent_search_path(os.pathsep.join(("/usr/bin", "/bin")))

    entries = path.split(os.pathsep)
    assert entries[1:3] == ["/usr/bin", "/bin"]
    assert "/opt/homebrew/bin" in entries
    assert "/usr/local/bin" in entries


def test_cli_agent_search_path_deduplicates_entries(monkeypatch) -> None:
    monkeypatch.setattr(cli_agents.Path, "home", lambda: Path("/home/tester"))

    path = get_cli_agent_search_path(
        os.pathsep.join(("/usr/bin", "/usr/bin", "/opt/homebrew/bin"))
    )
    entries = path.split(os.pathsep)

    assert entries.count("/usr/bin") == 1
    assert entries.count("/opt/homebrew/bin") == 1


def test_cli_agent_search_path_includes_user_bin_dirs(monkeypatch) -> None:
    home = Path("/home/tester")
    monkeypatch.setattr(cli_agents.Path, "home", lambda: home)

    entries = get_cli_agent_search_path("/usr/bin").split(os.pathsep)

    assert entries[0] == str(home / ".guildbotics/bin")
    assert str(home / ".local/bin") in entries
    assert str(home / "bin") in entries
    assert str(home / ".cargo/bin") in entries
    assert str(home / ".volta/bin") in entries


def test_cli_agent_search_path_falls_back_to_defpath_when_none(monkeypatch) -> None:
    default_path = str(Path("/defpath/bin"))
    monkeypatch.setattr(cli_agents.os, "defpath", default_path)
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.setattr(cli_agents.Path, "home", lambda: Path("/home/tester"))

    entries = get_cli_agent_search_path(None).split(os.pathsep)

    assert default_path in entries


def test_resolve_cli_agent_path_checks_managed_guildbotics_bin(
    tmp_path: Path, monkeypatch
) -> None:
    bin_dir = tmp_path / ".guildbotics/bin"
    bin_dir.mkdir(parents=True)
    executable = bin_dir / ("codex.exe" if os.name == "nt" else "codex")
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(cli_agents.Path, "home", lambda: tmp_path)

    assert Path(resolve_cli_agent_path("codex", "/usr/bin")) == executable


def test_managed_bin_wins_over_same_named_path_command(
    tmp_path: Path, monkeypatch
) -> None:
    managed_bin = tmp_path / ".guildbotics/bin"
    managed_bin.mkdir(parents=True)
    executable_name = "guildbotics.exe" if os.name == "nt" else "guildbotics"
    managed = managed_bin / executable_name
    managed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    managed.chmod(0o755)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake = fake_bin / executable_name
    fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(cli_agents.Path, "home", lambda: tmp_path)

    assert Path(resolve_cli_agent_path("guildbotics", str(fake_bin))) == managed


def test_resolve_cli_agent_path_returns_empty_when_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(cli_agents.Path, "home", lambda: tmp_path)

    assert resolve_cli_agent_path("does-not-exist", str(tmp_path)) == ""


def test_resolve_cli_agent_path_empty_executable() -> None:
    assert resolve_cli_agent_path("") == ""


def test_cli_agent_executable_uses_the_catalog() -> None:
    assert cli_agent_executable("grok") == "grok"
    assert cli_agent_executable("antigravity") == "agy"
    assert cli_agent_executable("unknown") == ""


def test_resolve_default_cli_executable_uses_the_catalog(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    mapping = tmp_path / "intelligences/cli_agent_mapping.yml"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text(
        "default: cli_agents/antigravity/default.yml\n", encoding="utf-8"
    )

    # The binary is named by the built-in catalog, not by the tool's yaml.
    assert resolve_default_cli_executable() == "agy"


def test_resolve_default_cli_executable_mapping_load_failure(monkeypatch) -> None:
    def _raise(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(cli_agents, "load_yaml_file", _raise)

    assert resolve_default_cli_executable() == ""


def test_resolve_default_cli_executable_missing_definition(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    mapping = tmp_path / "intelligences/cli_agent_mapping.yml"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text("default: cli_agents/ghost/default.yml\n", encoding="utf-8")

    # No agent named "ghost" in config or template.
    assert resolve_default_cli_executable() == ""
