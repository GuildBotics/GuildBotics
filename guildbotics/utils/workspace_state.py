from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from guildbotics.utils.advisory_lock import held_lock
from guildbotics.utils.fileio import (
    GUILDBOTICS_CONFIG_DIR,
    GUILDBOTICS_WORKSPACE_ROOT,
    apply_workspace_root,
    atomic_write_text,
    get_machine_state_path,
    get_workspace_root,
    workspace_root_from_config_dir,
)
from guildbotics.utils.safe_paths import (
    UnsafePathError,
    inspect_host_path,
    normalize_host_path,
    read_host_file,
    t,
)

ACTIVE_WORKSPACE_FILE = "active-workspace.json"
REGISTERED_WORKSPACES_FILE = "workspaces.json"


def registered_workspaces() -> tuple[Path, ...]:
    """Device-local locations, including workspaces that are not selected."""
    path = normalize_host_path(get_machine_state_path(REGISTERED_WORKSPACES_FILE))
    if not inspect_host_path(path, directory=False, missing=True).present:
        return ()
    try:
        payload = json.loads(read_host_file(path))
        if not isinstance(payload, list) or any(
            not isinstance(p, str) for p in payload
        ):
            raise ValueError("Expected a list of workspace paths")
        return tuple(normalize_host_path(Path(p)) for p in payload)
    except (OSError, ValueError) as exc:
        raise UnsafePathError(t("safe_paths.registry", path=path, reason=exc)) from exc


def register_workspace(workspace: Path) -> Path:
    """Remember a checked workspace location on this device."""
    workspace = inspect_host_path(workspace, missing=True).path
    _update_registry(workspace, remove=False)
    return workspace


def unregister_workspace(workspace: Path) -> None:
    """Forget a location only when the user explicitly removes its entry."""
    _update_registry(normalize_host_path(workspace), remove=True)


def _update_registry(workspace: Path, *, remove: bool) -> None:
    path = normalize_host_path(get_machine_state_path(REGISTERED_WORKSPACES_FILE))
    inspect_host_path(path.parent, create=True)
    with held_lock(path.with_suffix(".lock"), timeout=30):
        locations = list(registered_workspaces())
        if remove:
            locations = [p for p in locations if p != workspace]
        elif workspace not in locations:
            locations.append(workspace)
        else:
            return
        atomic_write_text(
            path, json.dumps([str(p) for p in locations], ensure_ascii=False) + "\n"
        )


@dataclass(frozen=True)
class WorkspaceState:
    workspace: Path
    config_dir: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace": str(self.workspace),
            "config_dir": str(self.config_dir),
        }


def active_workspace_file() -> Path:
    return get_machine_state_path(ACTIVE_WORKSPACE_FILE)


def workspace_state(workspace: Path) -> WorkspaceState:
    resolved = inspect_host_path(workspace, missing=True).path
    return WorkspaceState(
        workspace=resolved,
        config_dir=resolved / ".guildbotics" / "config",
    )


def write_active_workspace(workspace: Path) -> WorkspaceState:
    state = workspace_state(workspace)
    if not state.workspace.is_dir():
        raise NotADirectoryError(str(state.workspace))
    register_workspace(state.workspace)
    path = active_workspace_file()
    inspect_host_path(path.parent, create=True)
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
    try:
        workspace_exists = state.workspace.is_dir()
    except OSError:
        return None
    if not workspace_exists:
        return None
    return state


def apply_workspace_environment(state: WorkspaceState) -> None:
    apply_workspace_root(state.workspace)


def has_explicit_workspace_source() -> bool:
    """True when a workspace is already selected by an explicit env var."""
    if os.getenv(GUILDBOTICS_WORKSPACE_ROOT, "").strip():
        return True
    config_dir = os.getenv(GUILDBOTICS_CONFIG_DIR, "").strip()
    return bool(config_dir and workspace_root_from_config_dir(Path(config_dir)))


def apply_workspace_for_cli(workspace: Path | None = None) -> WorkspaceState | None:
    """Select the workspace for a CLI command.

    Only ``--workspace``, an explicit environment variable, or the persisted
    active workspace are accepted. The process cwd is never treated as a
    workspace root.
    """
    if workspace is not None:
        state = workspace_state(workspace)
        if not state.workspace.is_dir():
            raise NotADirectoryError(str(state.workspace))
        apply_workspace_environment(state)
        register_workspace(state.workspace)
        return state

    if has_explicit_workspace_source():
        apply_workspace_root(register_workspace(get_workspace_root()))
        return None

    active_state = read_active_workspace()
    if active_state is None:
        raise WorkspaceUnresolvedError(
            "No GuildBotics workspace is selected. Use --workspace, "
            "`guildbotics workspace use <path>`, or set GUILDBOTICS_WORKSPACE_ROOT."
        )
    apply_workspace_environment(active_state)
    register_workspace(active_state.workspace)
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
    }


class WorkspaceUnresolvedError(RuntimeError):
    """Raised when a CLI command cannot resolve a workspace."""
