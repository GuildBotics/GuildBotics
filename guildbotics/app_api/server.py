from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from types import FrameType
from typing import Literal

import uvicorn
from fastapi import FastAPI

from guildbotics.app_api.api import create_app
from guildbotics.app_api.auth import ServerAuth
from guildbotics.drivers.app_api_lock import (
    PARENT_PID_ENV,
    START_SERVICES_ENV,
    BackendAlreadyRunningError,
    hold_backend_lock,
)
from guildbotics.drivers.service_settings import read_service_settings
from guildbotics.utils.fileio import (
    GUILDBOTICS_CONFIG_DIR,
    GUILDBOTICS_WORKSPACE_ROOT,
    WorkspaceNotConfiguredError,
    apply_workspace_root,
    get_workspace_root,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.local_api import server_port
from guildbotics.utils.processes import pid_exists
from guildbotics.utils.safe_paths import UnsafePathError
from guildbotics.utils.workspace_state import (
    apply_workspace_environment,
    has_explicit_workspace_source,
    read_active_workspace,
    register_workspace,
)

#: The line on stdout that tells a launcher the server listens on the port.
PORT_NOTICE_PREFIX = "GUILDBOTICS_APP_API_PORT="


def _parent_is_alive(parent_pid: int) -> bool:
    return pid_exists(parent_pid)


def _watch_parent(parent_pid: int) -> None:
    """Exit the server once the process that launched it is gone.

    A force-killed ``guildbotics server start`` cannot stop its server, which would
    otherwise survive as an orphan holding the API port.
    """
    while True:
        if not _parent_is_alive(parent_pid):
            os._exit(0)
        time.sleep(1.0)


def _start_parent_watchdog() -> None:
    raw_pid = os.getenv(PARENT_PID_ENV)
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
        args=(parent_pid,),
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
    from guildbotics.environment.contract import validate_workspace_location

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


def _announce_port(port: int) -> None:
    """Tell the launcher, as the only line stdout ever carries, that this
    process holds and listens on the port.

    A launcher that waits for this line addresses the port only once its own
    child holds it, never a process that took the port first. Afterwards
    stdout is stderr, so nothing, not even a subprocess inheriting it, can
    write a second notice.
    """
    # Starting on a line of its own, whatever was written before it.
    print(f"\n{PORT_NOTICE_PREFIX}{port}", flush=True)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())


def _bind(port: int) -> socket.socket:
    """Bind the loopback port, never one another process already holds.

    uvicorn's own binding sets ``SO_REUSEADDR``, which on Windows lets a
    second socket take a port in use: two servers would answer on one port
    and write one set of credentials. Windows has ``SO_EXCLUSIVEADDRUSE`` for
    that; elsewhere ``SO_REUSEADDR`` only lets a restart reuse a port whose
    old connections are still closing.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
    except BaseException:
        sock.close()
        raise
    return sock


def _starts_with_server() -> bool:
    try:
        return read_service_settings().start_with_server
    except WorkspaceNotConfiguredError:
        # Nothing to start: no workspace, which the screens already report.
        return False


class _Server(uvicorn.Server):
    """A server that ends as ``guildbotics server stop`` ends it.

    The first Ctrl-C lets in-flight work finish and a second one cancels it;
    SIGTERM, which an OS sends once to stop its service before killing it,
    cancels at once. Nothing is re-raised after the exit, so an exit asked for
    is a successful one.
    """

    def __init__(self, config: uvicorn.Config, app: FastAPI, start_services: bool):
        super().__init__(config)
        self._app = app
        self._start_services = start_services
        # Counted here, not read from ``should_exit``, which an exit asked
        # through the API also sets.
        self._stage: Literal["", "draining", "cancelling"] = ""

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if not self.should_exit:
            threading.Thread(
                target=self._start, name="guildbotics-service-start", daemon=True
            ).start()

    def _start(self) -> None:
        """Start the services when asked, or when the saved settings say so."""
        try:
            if self._start_services or _starts_with_server():
                self._app.state.runtime.start_scheduler()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # The server stays up to show the failure and start them again:
            # on the screens, and in the output an OS service keeps.
            logging.getLogger("guildbotics").exception("The services did not start")
            print(f"The services did not start: {exc}", file=sys.stderr)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self._stage == "cancelling":
            # A third Ctrl-C skips a teardown that does not end.
            self.force_exit = self.force_exit or sig == signal.SIGINT
            return
        if sig == signal.SIGINT and not self._stage:
            self._stage = "draining"
            print(t("runtime.service_lock.stopping_after_work"), file=sys.stderr)
            self._app.state.shut_down(force=False)
            return
        if sig == signal.SIGINT:
            print(t("runtime.service_lock.cancelling"), file=sys.stderr)
        self._stage = "cancelling"
        self._app.state.shut_down(force=True)


def main() -> None:
    _start_parent_watchdog()
    try:
        with hold_backend_lock():
            _serve()
    except BackendAlreadyRunningError:
        sys.exit(t("runtime.service_lock.already_running"))


def _serve() -> None:
    config = uvicorn.Config(
        "",  # Built once the port is announced, below.
        # Loopback only: other machines reach the server through SSH
        # forwarding. The port is the one every program of the machine names.
        host="127.0.0.1",
        port=server_port(),
        access_log=False,
        # uvloop gives bun/node CLIs socketpair stdio, which can truncate their
        # output on exit. The standard loop uses synchronously flushed pipes.
        loop="asyncio",
    )
    # Only the process that holds the port touches the credentials of the
    # server on it, so a failed bind leaves a running server's sessions alone.
    with _bind(config.port) as sock:
        # Listen before announcing: a port that is bound but not listening can
        # still be bound by another socket with SO_REUSEADDR on Linux.
        sock.listen(config.backlog)
        port = sock.getsockname()[1]
        # Before the notice: the launcher reads the owner token once told.
        auth = ServerAuth(port)
        # Announced before anything that can wait on the user: loading the
        # workspace's secrets can stop on a keychain prompt, and the launcher's
        # deadline is for binding only. Connections wait in the backlog.
        _announce_port(port)
        workspace_problem = ""
        try:
            _restore_active_workspace()
        except (UnsafePathError, OSError) as exc:
            workspace_problem = str(exc)
            for key in (GUILDBOTICS_WORKSPACE_ROOT, GUILDBOTICS_CONFIG_DIR):
                os.environ.pop(key, None)
        app = create_app(
            auth=auth,
            restore_workspace_environment=True,
            workspace_problem=workspace_problem,
        )
        config.app = app
        uvicorn_server = _Server(
            config, app, os.environ.pop(START_SERVICES_ENV, "") == "1"
        )
        app.state.request_shutdown = lambda: setattr(
            uvicorn_server, "should_exit", True
        )
        uvicorn_server.run(sockets=[sock])
