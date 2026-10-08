from __future__ import annotations

import argparse
import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import uvicorn

from guildbotics.app_api.api import create_app
from guildbotics.utils.fileio import (
    GUILDBOTICS_CONFIG_DIR,
    GUILDBOTICS_WORKSPACE_ROOT,
    apply_workspace_root,
    get_workspace_root,
)
from guildbotics.utils.local_api import LocalApiEndpoint
from guildbotics.utils.processes import pid_exists
from guildbotics.utils.safe_paths import UnsafePathError
from guildbotics.utils.workspace_state import (
    apply_workspace_environment,
    has_explicit_workspace_source,
    read_active_workspace,
    register_workspace,
)

TOKEN_ENV = "GUILDBOTICS_APP_API_TOKEN"
ALLOWED_ORIGINS_ENV = "GUILDBOTICS_APP_API_ALLOWED_ORIGINS"
#: The one stdout line that tells the launching Desktop which port this server
#: bound. Every other stdout line is plain log output.
PORT_NOTICE_PREFIX = "GUILDBOTICS_APP_API_PORT="


def _parent_is_alive(parent_pid: int) -> bool:
    return pid_exists(parent_pid)


def _watch_parent(parent_pid: int, on_exit: Callable[[], None]) -> None:
    """Exit the sidecar once the parent (desktop app) process is gone.

    A force-killed desktop app cannot stop its sidecar, which would otherwise
    survive as an orphan holding the API port. Watching the desktop app PID
    covers that as well as a clean quit.
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
        name="guildbotics-parent-watchdog",
        daemon=True,
    )
    thread.start()


def _restore_active_workspace() -> Path:
    """Apply the selected workspace before constructing runtime services.

    An explicitly selected workspace (``GUILDBOTICS_WORKSPACE_ROOT`` or a
    workspace-shaped ``GUILDBOTICS_CONFIG_DIR``) wins over the persisted
    active workspace, matching the CLI resolution order.
    """
    from guildbotics.intelligences.agent_environment.contract import (
        validate_workspace_location,
    )

    startup_cwd = Path.cwd()
    if has_explicit_workspace_source():
        workspace = apply_workspace_root(
            register_workspace(validate_workspace_location(get_workspace_root()))
        )
        with contextlib.suppress(OSError):
            os.chdir(workspace)
        return workspace
    state = read_active_workspace()
    if state is None:
        return startup_cwd
    validate_workspace_location(state.workspace)
    register_workspace(state.workspace)
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


def _announce_port(port: int) -> None:
    """Tell the launcher the bound port as the only line stdout ever carries.

    The launcher passes port 0 and learns the port only from this line, so it
    never addresses a port this process does not hold. Afterwards stdout is
    stderr, so nothing, not even a subprocess inheriting it, can write a
    second notice.
    """
    # Starting on a line of its own, whatever was written before it.
    print(f"\n{PORT_NOTICE_PREFIX}{port}", flush=True)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the GuildBotics local app API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    token = _read_session_token()
    allowed_origins = _read_allowed_origins()
    config = uvicorn.Config(
        "",  # Built once the port is announced, below.
        host=args.host,
        port=args.port,
        access_log=False,
        # uvloop gives bun/node CLIs socketpair stdio, which can truncate their
        # output on exit. The standard loop uses synchronously flushed pipes.
        loop="asyncio",
    )
    # A failed bind must not replace a running Desktop's discovery record.
    with config.bind_socket() as sock:
        # Listen before announcing: a port that is bound but not listening can
        # still be bound by another socket with SO_REUSEADDR on Linux.
        sock.listen(config.backlog)
        # Announced before anything that can wait on the user: loading the
        # workspace's secrets can stop on a keychain prompt, and the launcher's
        # deadline is for binding only. Connections wait in the backlog.
        _announce_port(sock.getsockname()[1])
        workspace_problem = ""
        try:
            _restore_active_workspace()
        except (UnsafePathError, OSError) as exc:
            workspace_problem = str(exc)
            for key in (GUILDBOTICS_WORKSPACE_ROOT, GUILDBOTICS_CONFIG_DIR):
                os.environ.pop(key, None)
        app = create_app(
            session_token=token,
            allowed_origins=allowed_origins,
            restore_workspace_environment=True,
            workspace_problem=workspace_problem,
        )
        config.app = app
        endpoint = LocalApiEndpoint(
            port=sock.getsockname()[1],
            token=token,
            service_instance_id=app.state.runtime.system_service_run_id,
            workspace=app.state.runtime.get_config_status().workspace,
        )
        endpoint.publish()

        def workspace_changed(workspace: Path) -> None:
            endpoint.workspace = workspace
            endpoint.publish()

        app.state.runtime.on_workspace_changed = workspace_changed
        try:
            _start_parent_watchdog(endpoint.discard)
            uvicorn_server = uvicorn.Server(config)
            app.state.request_shutdown = lambda: setattr(
                uvicorn_server, "should_exit", True
            )
            uvicorn_server.run(sockets=[sock])
        finally:
            endpoint.discard()
