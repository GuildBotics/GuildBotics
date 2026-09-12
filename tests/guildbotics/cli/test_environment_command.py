"""The ``guildbotics environment`` group drives the snapshot and login modules."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

environment_cli = importlib.import_module("guildbotics.cli.environment")
from guildbotics.cli.environment import environment as environment_group
from guildbotics.cli import main
from guildbotics.intelligences.agent_environment import (
    provider_state,
    runtime,
    snapshot,
)
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentError,
    AgentEnvironmentHealth,
)
from guildbotics.intelligences.agent_environment.snapshot import (
    SnapshotStatus,
    snapshot_name,
)
from guildbotics.intelligences.agent_environment.toolchain import load_toolchain


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    root = tmp_path / "workspace"
    (root / ".guildbotics" / "config").mkdir(parents=True)
    monkeypatch.setattr(
        provider_state,
        "get_machine_state_path",
        lambda *parts: tmp_path.joinpath("data", *parts),
    )
    monkeypatch.setattr(
        runtime, "doctor", lambda: AgentEnvironmentHealth(True, "", "0.6.17")
    )
    return root


def _invoke(workspace: Path, *arguments: str) -> Any:
    return CliRunner().invoke(
        main, ["environment", "--workspace", str(workspace), *arguments]
    )


def test_status_reports_runtime_snapshot_and_logins(workspace: Path) -> None:
    result = _invoke(workspace, "status")

    assert result.exit_code == 0, result.output
    assert "runtime: available 0.6.17" in result.output
    assert (
        f"snapshot: missing {snapshot_name(load_toolchain())} "
        "(run `guildbotics environment build`)"
    ) in result.output
    assert "dns: 1.1.1.1, 8.8.8.8 -> 1.1.1.1, 8.8.8.8" in result.output
    assert (
        "codex: not logged in (run `guildbotics environment login codex`)"
        in result.output
    )
    assert (
        "grok: not logged in (run `guildbotics environment login grok`)"
        in result.output
    )


def test_status_json_has_the_same_facts(workspace: Path) -> None:
    result = _invoke(workspace, "status", "--format", "json")

    payload = json.loads(result.output)
    assert payload["runtime"] == {
        "available": True,
        "reason": "",
        "version": "0.6.17",
        "home": "",
    }
    assert payload["snapshot"]["state"] == "missing"
    assert payload["dns"] == {
        "declared": "1.1.1.1, 8.8.8.8",
        "nameservers": ["1.1.1.1", "8.8.8.8"],
        "problem": "",
    }
    tools = {tool["name"]: tool for tool in payload["tools"]}
    assert tools["codex"] == {
        "name": "codex",
        "label": "Codex",
        "provisioned": True,
        "logged_in": False,
    }
    assert tools["antigravity"]["provisioned"] is True


def test_commands_refuse_without_a_runtime(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime, "doctor", lambda: AgentEnvironmentHealth(False, "no hypervisor")
    )

    for command in ("build", "remove"):
        result = _invoke(workspace, command)
        assert result.exit_code != 0
        assert "no hypervisor" in result.output
    assert "runtime: unavailable: no hypervisor" in _invoke(workspace, "status").output


def test_build_runs_the_recipe_and_prints_its_lines(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_build(declaration: Any, *, on_line: Any, **_: Any) -> SnapshotStatus:
        on_line("[npm]")
        return SnapshotStatus("ready", "guildbotics-abc", Path("/snap"))

    monkeypatch.setattr(snapshot, "build_snapshot", fake_build)

    result = _invoke(workspace, "build")

    assert result.exit_code == 0, result.output
    assert "[npm]" in result.output
    assert "guildbotics-abc is ready" in result.output


def test_build_does_nothing_when_up_to_date_unless_forced(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = snapshot_name(load_toolchain())
    (snapshot.snapshots_dir(workspace) / name).mkdir(parents=True)
    built: list[str] = []

    async def fake_build(declaration: Any, *, on_line: Any, **_: Any) -> SnapshotStatus:
        built.append(name)
        return SnapshotStatus("ready", name, Path("/snap"))

    monkeypatch.setattr(snapshot, "build_snapshot", fake_build)

    assert "already up to date" in _invoke(workspace, "build").output
    assert built == []
    assert _invoke(workspace, "build", "--force").exit_code == 0
    assert built == [name]


def test_build_failure_is_an_error(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_build(*_: Any, **__: Any) -> SnapshotStatus:
        raise AgentEnvironmentError("Build step 'apt' failed with exit code 100.")

    monkeypatch.setattr(snapshot, "build_snapshot", fake_build)

    result = _invoke(workspace, "build")

    assert result.exit_code != 0
    assert "step 'apt' failed" in result.output


def test_login_needs_a_ready_snapshot(workspace: Path) -> None:
    result = _invoke(workspace, "login", "codex")

    assert result.exit_code != 0
    assert "The environment is missing; build it first" in result.output


def test_login_accepts_only_provisioned_tools(workspace: Path) -> None:
    result = _invoke(workspace, "login", "ghost")

    assert result.exit_code != 0
    assert (
        "Invalid value for '{codex|claude|grok|copilot|antigravity}'" in result.output
    )


def test_login_runs_inside_the_ready_snapshot_and_confirms_the_store(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = snapshot_name(load_toolchain())
    path = snapshot.snapshots_dir(workspace) / name
    path.mkdir(parents=True)
    calls: list[dict[str, Any]] = []

    async def fake_login(tool: Any, declaration: Any, **kwargs: Any) -> int:
        calls.append({"tool": tool.name, **kwargs})
        kwargs["write"]("Logged in\n")
        store = provider_state.provider_state_dir(tool)
        store.mkdir(parents=True)
        (store / "auth.json").write_text("{}")
        return 0

    monkeypatch.setattr(provider_state, "login", fake_login)

    result = _invoke(workspace, "login", "codex")

    assert result.exit_code == 0, result.output
    assert calls[0]["tool"] == "codex" and calls[0]["snapshot"] == path
    assert "Logged in" in result.output
    assert "Codex is logged in on this device." in result.output
    assert "codex: logged in" in _invoke(workspace, "status").output


def test_login_that_stores_nothing_is_an_error(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = snapshot.snapshots_dir(workspace) / snapshot_name(load_toolchain())
    path.mkdir(parents=True)

    async def fake_login(*_: Any, **__: Any) -> int:
        return 0

    monkeypatch.setattr(provider_state, "login", fake_login)

    result = _invoke(workspace, "login", "claude")

    assert result.exit_code != 0
    assert "stored no credentials" in result.output


def test_remove_lists_what_it_dropped(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_remove(*_: Any, **__: Any) -> list[str]:
        return ["guildbotics-old"]

    monkeypatch.setattr(snapshot, "remove_snapshots", fake_remove)

    assert "removed guildbotics-old" in _invoke(workspace, "remove").output


def test_environment_group_lists_its_commands() -> None:
    result = CliRunner().invoke(environment_group, ["--help"])

    assert result.exit_code == 0
    for command in ("build", "login", "remove", "status"):
        assert f"\n  {command}" in result.output
