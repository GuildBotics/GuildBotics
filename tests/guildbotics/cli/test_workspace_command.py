from __future__ import annotations

import json
from pathlib import Path
import pytest

from click.testing import CliRunner

from guildbotics.cli import main
from guildbotics.cli.workspace import workspace


@pytest.mark.parametrize("command", [["environment", "status"], ["secrets", "status"]])
@pytest.mark.parametrize("explicit", [True, False])
def test_every_cli_workspace_selection_refuses_exchange_locations(
    monkeypatch, command, explicit
):
    from guildbotics.utils.workspace_state import registered_workspaces

    target = Path.home() / "Documents/GuildBotics/workspace"
    target.mkdir(parents=True)
    before = registered_workspaces()
    args = [*command]
    if explicit:
        args = [command[0], "--workspace", str(target), *command[1:]]
    else:
        monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(target))
    result = CliRunner().invoke(main, args)
    assert result.exit_code != 0
    assert "inside granted directory" in result.output
    assert registered_workspaces() == before


def test_workspace_use_persists_active_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    project = tmp_path / "project"
    project.mkdir()
    runner = CliRunner()

    result = runner.invoke(workspace, ["use", str(project), "--format", "json"])

    assert result.exit_code == 0
    # Read as JSON rather than as text: a Windows path is full of backslashes,
    # and JSON escapes every one of them.
    assert json.loads(result.output)["workspace"] == str(project.resolve())
    assert (tmp_path / ".guildbotics" / "data" / "active-workspace.json").exists()


def test_workspace_current_fails_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    runner = CliRunner()

    result = runner.invoke(workspace, ["current"])

    assert result.exit_code != 0
    assert "No active GuildBotics workspace is configured" in result.output


def test_workspace_group_is_registered_on_main_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    runner = CliRunner()

    result = runner.invoke(main, ["workspace", "status", "--format", "json"])

    assert result.exit_code == 0
    assert '"configured": false' in result.output
