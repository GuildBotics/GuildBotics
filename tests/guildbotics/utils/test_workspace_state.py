from __future__ import annotations

import os

from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.workspace_state import (
    GUILDBOTICS_CONFIG_DIR,
    WorkspaceUnresolvedError,
    active_workspace_file,
    apply_workspace_environment,
    apply_workspace_for_cli,
    read_active_workspace,
    workspace_status_payload,
    write_active_workspace,
)
import pytest


def _set_home(monkeypatch, path) -> None:
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))


def test_write_and_read_active_workspace(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()

    written = write_active_workspace(workspace)
    loaded = read_active_workspace()

    assert active_workspace_file().exists()
    assert loaded == written
    assert loaded is not None
    assert loaded.workspace == workspace.resolve()
    assert loaded.config_dir == workspace.resolve() / ".guildbotics" / "config"
    assert not hasattr(loaded, "env_file")


def test_active_workspace_file_uses_machine_state_root(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _set_home(monkeypatch, home)

    assert active_workspace_file() == (
        home / ".guildbotics" / "data" / "active-workspace.json"
    )


def test_apply_workspace_environment_sets_config_and_root(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
    workspace = tmp_path / "project"
    workspace.mkdir()

    state = write_active_workspace(workspace)
    apply_workspace_environment(state)

    assert os.environ[GUILDBOTICS_CONFIG_DIR] == str(state.config_dir)
    assert os.environ[GUILDBOTICS_WORKSPACE_ROOT] == str(workspace.resolve())
    assert "GUILDBOTICS_ENV_FILE" not in os.environ
    assert "GUILDBOTICS_DATA_DIR" not in os.environ


def test_apply_workspace_for_cli_uses_active_when_no_explicit_source(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
    monkeypatch.delenv(GUILDBOTICS_WORKSPACE_ROOT, raising=False)
    cwd = tmp_path / "other"
    cwd.mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    state = write_active_workspace(workspace)

    applied = apply_workspace_for_cli(cwd=cwd)

    assert applied == state
    assert os.environ[GUILDBOTICS_CONFIG_DIR] == str(state.config_dir)
    assert os.environ[GUILDBOTICS_WORKSPACE_ROOT] == str(workspace.resolve())


def test_apply_workspace_for_cli_keeps_explicit_workspace_env(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    workspace = tmp_path / "explicit"
    workspace.mkdir()
    (workspace / ".guildbotics" / "config").mkdir(parents=True)
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(workspace))
    other = tmp_path / "project"
    other.mkdir()
    write_active_workspace(other)

    applied = apply_workspace_for_cli(cwd=tmp_path)

    assert applied is None
    assert os.environ[GUILDBOTICS_WORKSPACE_ROOT] == str(workspace.resolve())


def test_apply_workspace_for_cli_does_not_use_cwd(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
    monkeypatch.delenv(GUILDBOTICS_WORKSPACE_ROOT, raising=False)
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / ".guildbotics" / "config").mkdir(parents=True)

    with pytest.raises(WorkspaceUnresolvedError):
        apply_workspace_for_cli(cwd=cwd)


def test_workspace_status_payload_reports_missing_active_workspace(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)

    payload = workspace_status_payload()

    assert payload == {
        "configured": False,
        "state_file": str(active_workspace_file()),
    }
    assert "env_file" not in payload


def test_read_active_workspace_ignores_an_inaccessible_target(
    monkeypatch, tmp_path
) -> None:
    _set_home(monkeypatch, tmp_path / "home")
    target = tmp_path / "inaccessible"
    path = active_workspace_file()
    path.parent.mkdir(parents=True)
    path.write_text(f'{{"workspace": "{target.as_posix()}"}}', encoding="utf-8")
    original_is_dir = type(target).is_dir

    def is_dir(candidate):
        if candidate == target:
            raise PermissionError(str(candidate))
        return original_is_dir(candidate)

    monkeypatch.setattr(type(target), "is_dir", is_dir)

    assert read_active_workspace() is None
