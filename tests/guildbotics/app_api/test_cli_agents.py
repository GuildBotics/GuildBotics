from pathlib import Path

import pytest

from guildbotics.app_api import cli_agents
from guildbotics.app_api.cli_agents import (
    get_cli_agent_search_path,
    load_cli_agent_script,
    resolve_cli_agent_path,
    resolve_cli_executable,
    resolve_default_cli_executable,
)


def test_cli_agent_search_path_preserves_explicit_empty_path() -> None:
    assert get_cli_agent_search_path("") == ""


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


def test_resolve_cli_agent_path_checks_user_bin(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / ".local/bin"
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


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("codex", "codex"),
        ("codex exec", "codex"),
        ("FOO=bar gemini --yolo", "gemini"),
        ('claude "with quoted args"', "claude"),
        ("npx copilot --print", "copilot"),
        ("env CODEX_HOME=/tmp codex exec --json", "codex"),
        ("", ""),
        ("python run.py", ""),
    ],
)
def test_resolve_cli_executable_parses_script(script: str, expected: str) -> None:
    assert resolve_cli_executable(script) == expected


def test_resolve_cli_executable_returns_first_match() -> None:
    # CLI_AGENT_EXECUTABLES order is codex, gemini, claude, copilot.
    assert resolve_cli_executable("codex then gemini") == "codex"
    assert resolve_cli_executable("gemini then claude") == "gemini"


def _write_agent(config_root: Path, name: str, body: str) -> None:
    agent = config_root / "intelligences/cli_agents" / name
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(body, encoding="utf-8")


def test_load_cli_agent_script_returns_script(tmp_path: Path) -> None:
    _write_agent(tmp_path, "codex-cli.yml", "script: codex exec\n")

    assert load_cli_agent_script(tmp_path, "codex-cli.yml") == "codex exec"


def test_load_cli_agent_script_empty_info_file(tmp_path: Path) -> None:
    assert load_cli_agent_script(tmp_path, "") == ""


def test_load_cli_agent_script_missing_file(tmp_path: Path) -> None:
    assert load_cli_agent_script(tmp_path, "missing.yml") == ""


def test_load_cli_agent_script_malformed_yaml(tmp_path: Path) -> None:
    _write_agent(tmp_path, "broken.yml", "script: codex exec\n  bad: : :\n")

    assert load_cli_agent_script(tmp_path, "broken.yml") == ""


def test_load_cli_agent_script_missing_script_key(tmp_path: Path) -> None:
    _write_agent(tmp_path, "noscript.yml", "name: codex\n")

    assert load_cli_agent_script(tmp_path, "noscript.yml") == ""


def test_resolve_default_cli_executable_maps_mapping_to_executable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    mapping = tmp_path / "intelligences/cli_agent_mapping.yml"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text("default: gemini-cli.yml\n", encoding="utf-8")
    _write_agent(tmp_path, "gemini-cli.yml", "script: gemini --yolo\n")

    assert resolve_default_cli_executable() == "gemini"


def test_resolve_default_cli_executable_maps_codex(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    mapping = tmp_path / "intelligences/cli_agent_mapping.yml"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text("default: codex-cli.yml\n", encoding="utf-8")
    _write_agent(tmp_path, "codex-cli.yml", "script: codex exec\n")

    assert resolve_default_cli_executable() == "codex"


def test_resolve_default_cli_executable_mapping_load_failure(monkeypatch) -> None:
    def _raise(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(cli_agents, "load_yaml_file", _raise)

    assert resolve_default_cli_executable() == ""


def test_resolve_default_cli_executable_missing_definition(
    tmp_path: Path, monkeypatch
) -> None:
    # Mapping resolves but the referenced definition yields no script,
    # so no executable can be inferred.
    monkeypatch.setattr(
        cli_agents, "load_yaml_file", lambda _file: {"default": "ghost-cli.yml"}
    )
    monkeypatch.setattr(cli_agents, "load_cli_agent_script", lambda *_a: "")

    assert resolve_default_cli_executable() == ""
