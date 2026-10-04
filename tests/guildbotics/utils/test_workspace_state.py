from __future__ import annotations

import os

from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.workspace_state import (
    register_workspace,
    registered_workspaces,
    unregister_workspace,
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


@pytest.mark.parametrize("payload", ["{", "{}", "[1]", '["/bad/../path"]'])
def test_invalid_registry_is_a_descriptive_safe_path_error(
    tmp_path, monkeypatch, payload
):
    from guildbotics.utils.safe_paths import UnsafePathError
    from guildbotics.utils.workspace_state import REGISTERED_WORKSPACES_FILE

    _set_home(monkeypatch, tmp_path / "home")
    registry = active_workspace_file().with_name(REGISTERED_WORKSPACES_FILE)
    registry.parent.mkdir(parents=True)
    registry.write_text(payload)
    with pytest.raises(UnsafePathError, match="registry"):
        registered_workspaces()


@pytest.mark.parametrize("source", ["argument", "environment", "active"])
def test_each_cli_selection_registers_the_workspace_without_prior_registry(
    tmp_path, monkeypatch, source
):
    _set_home(monkeypatch, tmp_path / "home")
    workspace = tmp_path / "selected"
    workspace.mkdir()
    monkeypatch.delenv(GUILDBOTICS_WORKSPACE_ROOT, raising=False)
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
    if source == "environment":
        monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(workspace))
    elif source == "active":
        write_active_workspace(workspace)
        active_workspace_file().with_name("workspaces.json").unlink()
    apply_workspace_for_cli(workspace if source == "argument" else None)
    assert registered_workspaces() == (workspace,)


def _set_home(monkeypatch, path) -> None:
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))


def test_registry_preserves_unselected_workspaces_and_only_forgets_locations(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path / "home")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    write_active_workspace(first)
    write_active_workspace(second)
    register_workspace(first)
    assert registered_workspaces() == (first, second)
    unregister_workspace(first)
    assert registered_workspaces() == (second,)
    assert first.is_dir() and second.is_dir()
    assert read_active_workspace().workspace == second


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
    workspace = tmp_path / "project"
    workspace.mkdir()
    state = write_active_workspace(workspace)

    applied = apply_workspace_for_cli()

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

    applied = apply_workspace_for_cli()

    assert applied is None
    assert os.environ[GUILDBOTICS_WORKSPACE_ROOT] == str(workspace.resolve())


def test_apply_workspace_for_cli_does_not_use_cwd(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
    monkeypatch.delenv(GUILDBOTICS_WORKSPACE_ROOT, raising=False)
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / ".guildbotics" / "config").mkdir(parents=True)
    monkeypatch.chdir(cwd)

    with pytest.raises(WorkspaceUnresolvedError):
        apply_workspace_for_cli()


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
