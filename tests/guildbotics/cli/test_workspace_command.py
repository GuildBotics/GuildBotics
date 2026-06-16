from __future__ import annotations

from click.testing import CliRunner

from guildbotics.cli import main
from guildbotics.cli.workspace import workspace


def test_workspace_use_persists_active_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"
    project.mkdir()
    runner = CliRunner()

    result = runner.invoke(workspace, ["use", str(project), "--format", "json"])

    assert result.exit_code == 0
    assert f'"workspace": "{project.resolve()}"' in result.output
    assert (tmp_path / ".guildbotics" / "data" / "active-workspace.json").exists()


def test_workspace_current_fails_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()

    result = runner.invoke(workspace, ["current"])

    assert result.exit_code != 0
    assert "No active GuildBotics workspace is configured" in result.output


def test_workspace_group_is_registered_on_main_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()

    result = runner.invoke(main, ["workspace", "status", "--format", "json"])

    assert result.exit_code == 0
    assert '"configured": false' in result.output
