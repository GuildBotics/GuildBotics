from __future__ import annotations

import argparse
import contextlib
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import uvicorn

from guildbotics.app_api.api import create_app
from guildbotics.utils.fileio import apply_workspace_root, get_workspace_root
from guildbotics.utils.local_api import LocalApiEndpoint
from guildbotics.utils.processes import pid_exists
from guildbotics.utils.workspace_state import (
    apply_workspace_environment,
    has_explicit_workspace_source,
    read_active_workspace,
)

TOKEN_ENV = "GUILDBOTICS_APP_API_TOKEN"
ALLOWED_ORIGINS_ENV = "GUILDBOTICS_APP_API_ALLOWED_ORIGINS"


def _parent_is_alive(parent_pid: int) -> bool:
    return pid_exists(parent_pid)


def _watch_parent(parent_pid: int, on_exit: Callable[[], None]) -> None:
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
            with contextlib.suppress(OSError):
                on_exit()
            os._exit(0)
        time.sleep(1.0)


def _start_parent_watchdog(on_exit: Callable[[], None]) -> None:
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
        target=_watch_parent,
        args=(parent_pid, on_exit),
        name="parent-watchdog",
        daemon=True,
    )
    thread.start()


def _restore_active_workspace() -> Path:
    """Apply the selected workspace before constructing runtime services.

    An explicitly selected workspace (``GUILDBOTICS_WORKSPACE_ROOT`` or a
    workspace-shaped ``GUILDBOTICS_CONFIG_DIR``) wins over the persisted
    active workspace, matching the CLI resolution order.
    """
    startup_cwd = Path.cwd()
    if has_explicit_workspace_source():
        workspace = apply_workspace_root(get_workspace_root())
        with contextlib.suppress(OSError):
            os.chdir(workspace)
        return workspace
    state = read_active_workspace()
    if state is None:
        return startup_cwd
    try:
        os.chdir(state.workspace)
    except OSError:
        return startup_cwd
    apply_workspace_environment(state)
    return state.workspace


def _read_session_token() -> str:
    """Consume the session token the launcher put in the environment.

    The token is never accepted on the command line and never printed: argv is
    world-readable through ``ps`` on a shared host, and a printed token spreads
    into logs and screenshots. Host CLI callers discover it through a private
    device-local file. AI CLI agents run in microVMs without the host environment
    or machine-state directory mounted. Consume the launch-only variable so it
    does not linger in unrelated host subprocesses. Every launcher
    in this repository mints its own token, so a missing one is a wiring bug
    rather than something to paper over with a generated value.
    """
    token = os.environ.pop(TOKEN_ENV, "").strip()
    if not token:
        raise SystemExit(
            f"{TOKEN_ENV} must be set to the session token for the local app API."
        )
    return token


def _read_allowed_origins() -> list[str]:
    """Return the extra browser origins allowed by CORS, as set by the launcher.

    Only the launcher knows which port the browser preview is served from, so
    the allowlist is injected instead of being guessed by the server.
    """
    raw = os.getenv(ALLOWED_ORIGINS_ENV, "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the GuildBotics local app API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    token = _read_session_token()
    allowed_origins = _read_allowed_origins()
    _restore_active_workspace()

    app = create_app(
        session_token=token,
        allowed_origins=allowed_origins,
        restore_workspace_environment=True,
    )
    endpoint = LocalApiEndpoint(
        port=args.port,
        token=token,
        pid=os.getpid(),
        service_instance_id=app.state.runtime.system_service_run_id,
        workspace=app.state.runtime.get_config_status().workspace,
    )

    def workspace_changed(workspace: Path) -> None:
        endpoint.workspace = workspace
        endpoint.publish()

    app.state.runtime.on_workspace_changed = workspace_changed
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        access_log=False,
        # uvloop gives bun/node CLIs socketpair stdio, which can truncate their
        # output on exit. The standard loop uses synchronously flushed pipes.
        loop="asyncio",
    )
    # A failed bind must not replace a running Desktop's discovery record.
    with config.bind_socket() as sock:
        endpoint.port = sock.getsockname()[1]
        endpoint.publish()
        try:
            _start_parent_watchdog(endpoint.discard)
            uvicorn.Server(config).run(sockets=[sock])
        finally:
            endpoint.discard()
