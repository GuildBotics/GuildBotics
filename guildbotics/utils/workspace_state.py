from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from guildbotics.utils.env_loader import (
    GUILDBOTICS_ENV_FILE,
    resolve_guildbotics_env_file,
)
from guildbotics.utils.fileio import (
    GUILDBOTICS_DATA_DIR,
    apply_workspace_data_root,
    get_machine_state_path,
)

GUILDBOTICS_CONFIG_DIR = "GUILDBOTICS_CONFIG_DIR"
ACTIVE_WORKSPACE_FILE = "active-workspace.json"


@dataclass(frozen=True)
class WorkspaceState:
    workspace: Path
    config_dir: Path
    env_file: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace": str(self.workspace),
            "config_dir": str(self.config_dir),
            "env_file": str(self.env_file),
        }


def active_workspace_file() -> Path:
    return get_machine_state_path(ACTIVE_WORKSPACE_FILE)


def workspace_state(workspace: Path) -> WorkspaceState:
    resolved = workspace.expanduser().resolve(strict=False)
    return WorkspaceState(
        workspace=resolved,
        config_dir=resolved / ".guildbotics" / "config",
        env_file=resolved / ".env",
    )


def write_active_workspace(workspace: Path) -> WorkspaceState:
    state = workspace_state(workspace)
    if not state.workspace.is_dir():
        raise NotADirectoryError(str(state.workspace))
    path = active_workspace_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return state


def read_active_workspace() -> WorkspaceState | None:
    path = active_workspace_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    workspace_value = payload.get("workspace")
    if not isinstance(workspace_value, str) or not workspace_value.strip():
        return None
    state = workspace_state(Path(workspace_value))
    if not state.workspace.is_dir():
        return None
    return state


def apply_workspace_environment(
    state: WorkspaceState, *, inherited_data_dir: str | None = None
) -> None:
    os.environ[GUILDBOTICS_CONFIG_DIR] = str(state.config_dir)
    if state.env_file.is_file():
        os.environ[GUILDBOTICS_ENV_FILE] = str(state.env_file)
    else:
        os.environ.pop(GUILDBOTICS_ENV_FILE, None)
    apply_workspace_data_root(
        state.workspace,
        state.env_file,
        inherited_data_dir=inherited_data_dir,
    )


def has_primary_config_source(cwd: Path | None = None) -> bool:
    if os.getenv(GUILDBOTICS_CONFIG_DIR, "").strip():
        return True
    if cwd is None:
        cwd = Path.cwd()
    return (cwd / ".guildbotics" / "config").exists()


def apply_workspace_for_cli(
    workspace: Path | None = None,
    *,
    cwd: Path | None = None,
    inherited_data_dir: str | None = None,
) -> WorkspaceState | None:
    if inherited_data_dir is None:
        inherited_data_dir = os.getenv(GUILDBOTICS_DATA_DIR, "").strip() or None
    if workspace is not None:
        state = workspace_state(workspace)
        if not state.workspace.is_dir():
            raise NotADirectoryError(str(state.workspace))
        apply_workspace_environment(state, inherited_data_dir=inherited_data_dir)
        return state

    if has_primary_config_source(cwd):
        root = cwd if cwd is not None else Path.cwd()
        env_file = resolve_guildbotics_env_file(root, prefer_env_file=True)
        apply_workspace_data_root(
            root,
            env_file,
            inherited_data_dir=inherited_data_dir,
        )
        return None

    active_state = read_active_workspace()
    if active_state is not None:
        apply_workspace_environment(active_state, inherited_data_dir=inherited_data_dir)
    else:
        root = cwd if cwd is not None else Path.cwd()
        env_file = resolve_guildbotics_env_file(root, prefer_env_file=True)
        apply_workspace_data_root(
            root,
            env_file,
            inherited_data_dir=inherited_data_dir,
        )
    return active_state


def workspace_status_payload(state: WorkspaceState | None = None) -> dict[str, Any]:
    if state is None:
        state = read_active_workspace()
    if state is None:
        return {
            "configured": False,
            "state_file": str(active_workspace_file()),
        }
    return {
        "configured": True,
        "state_file": str(active_workspace_file()),
        "workspace": str(state.workspace),
        "workspace_exists": state.workspace.is_dir(),
        "config_dir": str(state.config_dir),
        "config_dir_exists": state.config_dir.is_dir(),
        "env_file": str(state.env_file),
        "env_file_exists": state.env_file.is_file(),
    }
