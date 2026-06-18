from __future__ import annotations

import os

from guildbotics.utils.env_loader import GUILDBOTICS_ENV_FILE
from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR
from guildbotics.utils.workspace_state import (
    GUILDBOTICS_CONFIG_DIR,
    active_workspace_file,
    apply_workspace_environment,
    apply_workspace_for_cli,
    read_active_workspace,
    workspace_status_payload,
    write_active_workspace,
)


def _clear_workspace_data_dir(monkeypatch) -> None:
    monkeypatch.delenv(GUILDBOTICS_DATA_DIR, raising=False)


def test_write_and_read_active_workspace(monkeypatch, tmp_path):
    _clear_workspace_data_dir(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "project"
    workspace.mkdir()

    written = write_active_workspace(workspace)
    loaded = read_active_workspace()

    assert active_workspace_file().exists()
    assert loaded == written
    assert loaded is not None
    assert loaded.workspace == workspace.resolve()
    assert loaded.config_dir == workspace.resolve() / ".guildbotics" / "config"
    assert loaded.env_file == workspace.resolve() / ".env"


def test_active_workspace_file_uses_machine_state_root(monkeypatch, tmp_path):
    home = tmp_path / "home"
    data_dir = tmp_path / "custom-data"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(data_dir))

    assert active_workspace_file() == (
        home / ".guildbotics" / "data" / "active-workspace.json"
    )


def test_apply_workspace_environment_sets_config_and_env(monkeypatch, tmp_path):
    # Sandbox HOME so write_active_workspace targets the temp dir instead of the
    # real ~/.guildbotics/data/active-workspace.json (writing there would clobber
    # the developer's active workspace when the suite runs).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
    monkeypatch.delenv(GUILDBOTICS_ENV_FILE, raising=False)
    _clear_workspace_data_dir(monkeypatch)
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".env").write_text("TOKEN=value\n", encoding="utf-8")

    state = write_active_workspace(workspace)
    apply_workspace_environment(state)

    assert os.environ[GUILDBOTICS_CONFIG_DIR] == str(state.config_dir)
    assert os.environ[GUILDBOTICS_ENV_FILE] == str(state.env_file)
    assert os.environ[GUILDBOTICS_DATA_DIR] == str(
        workspace.resolve() / ".guildbotics" / "data"
    )


def test_apply_workspace_for_cli_uses_active_when_no_primary_source(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
    monkeypatch.delenv(GUILDBOTICS_ENV_FILE, raising=False)
    _clear_workspace_data_dir(monkeypatch)
    cwd = tmp_path / "other"
    cwd.mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    state = write_active_workspace(workspace)

    applied = apply_workspace_for_cli(cwd=cwd)

    assert applied == state
    assert os.environ[GUILDBOTICS_CONFIG_DIR] == str(state.config_dir)
    assert os.environ[GUILDBOTICS_DATA_DIR] == str(
        workspace.resolve() / ".guildbotics" / "data"
    )


def test_apply_workspace_for_cli_keeps_existing_primary_source(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_workspace_data_dir(monkeypatch)
    existing = tmp_path / "explicit-config"
    monkeypatch.setenv(GUILDBOTICS_CONFIG_DIR, str(existing))
    workspace = tmp_path / "project"
    workspace.mkdir()
    write_active_workspace(workspace)

    applied = apply_workspace_for_cli(cwd=tmp_path)

    assert applied is None
    assert os.environ[GUILDBOTICS_CONFIG_DIR] == str(existing)
    assert os.environ[GUILDBOTICS_DATA_DIR] == str(
        tmp_path.resolve() / ".guildbotics" / "data"
    )


def test_apply_workspace_for_cli_prefers_workspace_env_data_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(tmp_path / "inherited-data"))
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "GUILDBOTICS_DATA_DIR=workspace-data\n",
        encoding="utf-8",
    )

    applied = apply_workspace_for_cli(workspace)

    assert applied is not None
    assert os.environ[GUILDBOTICS_DATA_DIR] == str(
        (tmp_path / "workspace-data").resolve(strict=False)
    )


def test_workspace_status_payload_reports_missing_active_workspace(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))

    payload = workspace_status_payload()

    assert payload == {
        "configured": False,
        "state_file": str(active_workspace_file()),
    }
