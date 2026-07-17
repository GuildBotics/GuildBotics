from __future__ import annotations

import json
import os
from pathlib import Path

from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.app_api.server import _restore_active_workspace
from guildbotics.utils.env_loader import GUILDBOTICS_ENV_FILE
from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR
from guildbotics.utils.workspace_state import (
    GUILDBOTICS_CONFIG_DIR,
    active_workspace_file,
    write_active_workspace,
)


def _isolate_runtime_environment(monkeypatch) -> None:
    for key in (
        GUILDBOTICS_CONFIG_DIR,
        GUILDBOTICS_ENV_FILE,
        GUILDBOTICS_DATA_DIR,
        "WORKSPACE_MARKER",
    ):
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)


def test_restore_active_workspace_applies_backend_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    startup = tmp_path / "startup"
    workspace = tmp_path / "selected"
    startup.mkdir()
    workspace.mkdir()
    (workspace / ".env").write_text(
        "GUILDBOTICS_DATA_DIR=runtime-data\nWORKSPACE_MARKER=selected\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(startup)
    _isolate_runtime_environment(monkeypatch)
    write_active_workspace(workspace)

    restored = _restore_active_workspace(inherited_data_dir=None)
    AppRuntime(
        EventBus(),
        inherited_data_dir=None,
        load_workspace_environment=True,
    )

    assert restored == workspace
    assert Path.cwd() == workspace
    assert os.environ[GUILDBOTICS_CONFIG_DIR] == str(
        workspace / ".guildbotics" / "config"
    )
    assert os.environ[GUILDBOTICS_ENV_FILE] == str(workspace / ".env")
    assert os.environ[GUILDBOTICS_DATA_DIR] == str(workspace / "runtime-data")
    assert os.environ["WORKSPACE_MARKER"] == "selected"


def test_restored_runtime_keeps_original_data_override_across_switches(
    tmp_path: Path, monkeypatch
) -> None:
    startup = tmp_path / "startup"
    selected = tmp_path / "selected"
    other = tmp_path / "other"
    inherited_data = tmp_path / "inherited-data"
    for path in (startup, selected, other):
        path.mkdir()
    (selected / ".env").write_text(
        "GUILDBOTICS_DATA_DIR=selected-data\n", encoding="utf-8"
    )
    monkeypatch.chdir(startup)
    _isolate_runtime_environment(monkeypatch)
    write_active_workspace(selected)

    _restore_active_workspace(inherited_data_dir=str(inherited_data))
    runtime = AppRuntime(
        EventBus(),
        inherited_data_dir=str(inherited_data),
        load_workspace_environment=True,
    )
    runtime.set_workspace(other)

    assert os.environ[GUILDBOTICS_DATA_DIR] == str(inherited_data)


def test_restore_active_workspace_keeps_startup_cwd_when_unconfigured(
    tmp_path: Path, monkeypatch
) -> None:
    startup = tmp_path / "startup"
    startup.mkdir()
    monkeypatch.chdir(startup)

    restored = _restore_active_workspace()

    assert restored == startup
    assert Path.cwd() == startup


def test_restore_active_workspace_keeps_startup_cwd_when_target_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    startup = tmp_path / "startup"
    startup.mkdir()
    monkeypatch.chdir(startup)
    path = active_workspace_file()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"workspace": str(tmp_path / "missing")}), encoding="utf-8"
    )

    restored = _restore_active_workspace()

    assert restored == startup
    assert Path.cwd() == startup
