from pathlib import Path

from guildbotics.intelligences import cli_agents
from guildbotics.intelligences.cli_agents import (
    discover_cli_agents,
    get_cli_agent_search_path,
    resolve_cli_agent_path,
    resolve_default_cli_executable,
)

CUSTOM_ORDER = 5
DEFAULT_ORDER = 1000
TEMPLATE_CLI_AGENT_NAMES = (
    "antigravity-cli.yml",
    "claude-cli.yml",
    "codex-cli.yml",
    "copilot-cli.yml",
)


def test_cli_agent_search_path_preserves_explicit_empty_path() -> None:
    assert get_cli_agent_search_path("") == ""


def test_rate_limit_marker_templates_use_json_serialization() -> None:
    template_dir = (
        Path(__file__).parents[3] / "guildbotics/templates/intelligences/cli_agents"
    )

    for name in TEMPLATE_CLI_AGENT_NAMES:
        script = (template_dir / name).read_text(encoding="utf-8")

        assert "json.dumps(payload" in script
        assert 'retry_after_text":"$retry_after_text' not in script
        assert 'retry_after_timezone":"$retry_after_timezone' not in script


def test_cli_agent_search_path_falls_back_for_ambient_empty_path(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")

    path = get_cli_agent_search_path()

    entries = path.split(":")
    assert "/usr/bin" in entries
    assert "/opt/homebrew/bin" in entries


def test_cli_agent_search_path_adds_gui_app_fallbacks() -> None:
    path = get_cli_agent_search_path("/usr/bin:/bin")

    entries = path.split(":")
    assert entries[:2] == ["/usr/bin", "/bin"]
    assert "/opt/homebrew/bin" in entries
    assert "/usr/local/bin" in entries


def test_cli_agent_search_path_deduplicates_entries(monkeypatch) -> None:
    monkeypatch.setattr(cli_agents.Path, "home", lambda: Path("/home/tester"))

    path = get_cli_agent_search_path("/usr/bin:/usr/bin:/opt/homebrew/bin")
    entries = path.split(":")

    assert entries.count("/usr/bin") == 1
    assert entries.count("/opt/homebrew/bin") == 1


def test_cli_agent_search_path_includes_user_bin_dirs(monkeypatch) -> None:
    monkeypatch.setattr(cli_agents.Path, "home", lambda: Path("/home/tester"))

    entries = get_cli_agent_search_path("/usr/bin").split(":")

    assert "/home/tester/.guildbotics/bin" in entries
    assert "/home/tester/.local/bin" in entries
    assert "/home/tester/bin" in entries
    assert "/home/tester/.cargo/bin" in entries
    assert "/home/tester/.volta/bin" in entries


def test_cli_agent_search_path_falls_back_to_defpath_when_none(monkeypatch) -> None:
    monkeypatch.setattr(cli_agents.os, "defpath", "/defpath/bin")
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.setattr(cli_agents.Path, "home", lambda: Path("/home/tester"))

    entries = get_cli_agent_search_path(None).split(":")

    assert "/defpath/bin" in entries


def test_resolve_cli_agent_path_checks_managed_guildbotics_bin(
    tmp_path: Path, monkeypatch
) -> None:
    bin_dir = tmp_path / ".guildbotics/bin"
    bin_dir.mkdir(parents=True)
    executable = bin_dir / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(cli_agents.Path, "home", lambda: tmp_path)

    assert resolve_cli_agent_path("codex", "/usr/bin") == str(executable)


def test_resolve_cli_agent_path_returns_empty_when_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(cli_agents.Path, "home", lambda: tmp_path)

    assert resolve_cli_agent_path("does-not-exist", str(tmp_path)) == ""


def test_resolve_cli_agent_path_empty_executable() -> None:
    assert resolve_cli_agent_path("") == ""


def _write_agent(config_root: Path, name: str, body: str) -> None:
    agent = config_root / "intelligences/cli_agents" / name
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(body, encoding="utf-8")


def test_discover_cli_agents_reads_metadata(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        "myagent-cli.yml",
        "label: My Agent\norder: 5\nexecutable: mybin\nscript: mybin\n",
    )

    agents = {agent.name: agent for agent in discover_cli_agents(tmp_path)}

    assert agents["myagent"].label == "My Agent"
    assert agents["myagent"].order == CUSTOM_ORDER
    assert agents["myagent"].executable == "mybin"


def test_discover_cli_agents_defaults_to_name_and_sorts(tmp_path: Path) -> None:
    _write_agent(tmp_path, "zeta-cli.yml", "script: zeta\n")

    agents = discover_cli_agents(tmp_path)
    zeta = next(agent for agent in agents if agent.name == "zeta")

    # Missing metadata falls back to the agent name / a low priority order.
    assert zeta.label == "zeta"
    assert zeta.executable == "zeta"
    assert zeta.order == DEFAULT_ORDER
    assert [agent.order for agent in agents] == sorted(agent.order for agent in agents)


def test_discover_cli_agents_config_overrides_template(tmp_path: Path) -> None:
    # antigravity ships as a template; a config-scoped file wins.
    _write_agent(
        tmp_path,
        "antigravity-cli.yml",
        "label: Custom\norder: 1\nexecutable: custom\nscript: custom\n",
    )

    agents = {agent.name: agent for agent in discover_cli_agents(tmp_path)}

    assert agents["antigravity"].label == "Custom"
    assert agents["antigravity"].executable == "custom"


def test_discover_cli_agents_tolerates_malformed_yaml(tmp_path: Path) -> None:
    _write_agent(tmp_path, "broken-cli.yml", "label: [broken\n")

    agents = {agent.name: agent for agent in discover_cli_agents(tmp_path)}

    assert agents["broken"].label == "broken"
    assert agents["broken"].order == DEFAULT_ORDER
    assert agents["broken"].executable == "broken"


def test_resolve_default_cli_executable_returns_declared_executable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    mapping = tmp_path / "intelligences/cli_agent_mapping.yml"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text("default: antigravity-cli.yml\n", encoding="utf-8")
    _write_agent(tmp_path, "antigravity-cli.yml", "executable: agy\nscript: agy\n")

    assert resolve_default_cli_executable() == "agy"


def test_resolve_default_cli_executable_uses_template_when_not_overridden(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    mapping = tmp_path / "intelligences/cli_agent_mapping.yml"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text("default: antigravity-cli.yml\n", encoding="utf-8")

    # No config-scoped agent file: the shipped template declares ``executable: agy``.
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
    mapping.write_text("default: ghost-cli.yml\n", encoding="utf-8")

    # No agent named "ghost" in config or template.
    assert resolve_default_cli_executable() == ""
