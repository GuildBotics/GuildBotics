from __future__ import annotations

import argparse
import os
import secrets
import threading
import time
from pathlib import Path

import uvicorn

from guildbotics.app_api.api import create_app
from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR
from guildbotics.utils.workspace_state import (
    apply_workspace_environment,
    read_active_workspace,
)


def _parent_is_alive(parent_pid: int) -> bool:
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by someone else; treat as alive.
        return True
    except OSError:
        return False
    return True


def _watch_parent(parent_pid: int) -> None:
    """Exit the sidecar once the parent (desktop app) process is gone.

    The packaged sidecar is a PyInstaller one-file binary, so the desktop host
    actually spawns a bootloader process that re-executes the real worker as a
    child. Killing the bootloader does not reliably terminate that worker, which
    would otherwise survive as an orphan holding the API port. Watching the
    desktop app PID directly covers both a clean quit and a force-kill of the
    app.
    """
    while True:
        if not _parent_is_alive(parent_pid):
            os._exit(0)
        time.sleep(1.0)


def _start_parent_watchdog() -> None:
    raw_pid = os.getenv("GUILDBOTICS_APP_API_PARENT_PID")
    if not raw_pid:
        return
    try:
        parent_pid = int(raw_pid)
    except ValueError:
        return
    if parent_pid <= 1:
        return
    thread = threading.Thread(
        target=_watch_parent, args=(parent_pid,), name="parent-watchdog", daemon=True
    )
    thread.start()


def _restore_active_workspace(*, inherited_data_dir: str | None = None) -> Path:
    """Apply the persisted workspace before constructing runtime services."""
    startup_cwd = Path.cwd()
    state = read_active_workspace()
    if state is None:
        return startup_cwd
    try:
        os.chdir(state.workspace)
    except OSError:
        return startup_cwd
    apply_workspace_environment(state, inherited_data_dir=inherited_data_dir)
    return state.workspace


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the GuildBotics local app API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.getenv("GUILDBOTICS_APP_API_TOKEN"))
    args = parser.parse_args()

    _start_parent_watchdog()
    inherited_data_dir = os.getenv(GUILDBOTICS_DATA_DIR, "").strip() or None
    _restore_active_workspace(inherited_data_dir=inherited_data_dir)

    token = args.token or secrets.token_urlsafe(32)
    print(f"GUILDBOTICS_APP_API_TOKEN={token}", flush=True)
    uvicorn.run(
        create_app(
            session_token=token,
            restore_workspace_environment=True,
            inherited_data_dir=inherited_data_dir,
        ),
        host=args.host,
        port=args.port,
        access_log=False,
    )
