"""Run the server of this checkout, and end it."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Any

import click
import httpx

from guildbotics.cli._options import selected_workspace
from guildbotics.drivers.app_api_lock import (
    PARENT_PID_ENV,
    START_SERVICES_ENV,
    inspect_backend_lock,
)
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.i18n_tool import get_language, t
from guildbotics.utils.local_api import connect_local_api
from guildbotics.utils.processes import force_terminate_pid, pid_exists

POLL_SECONDS = 0.1


class BackendUnavailableError(RuntimeError):
    pass


def _call(path: str, *, payload: dict[str, Any], timeout: float) -> None:
    with connect_local_api(
        headers={"Accept-Language": get_language()}, timeout=timeout
    ) as client:
        if client is None:
            raise BackendUnavailableError(t("runtime.service_lock.api_unavailable"))
        response = client.post(path, json=payload)
        if response.is_error:
            try:
                error = response.json()
            except ValueError:
                error = None
            message = error.get("message") if isinstance(error, dict) else None
            if isinstance(message, str) and message:
                raise httpx.HTTPStatusError(
                    message, request=response.request, response=response
                )
        response.raise_for_status()


def _backend_command() -> list[str]:
    return [sys.executable, "-m", "guildbotics.app_api"]


@click.command()
def start() -> None:
    """Run the server and start its services with the settings saved on the
    Service screen.

    It runs until `guildbotics stop`. Ctrl-C asks the same: the first lets
    in-flight work finish, the second cancels it. SIGTERM, which an OS sends
    to stop its service, cancels it at once.
    """
    environment = {
        **os.environ,
        GUILDBOTICS_WORKSPACE_ROOT: str(selected_workspace()),
        START_SERVICES_ENV: "1",
        PARENT_PID_ENV: str(os.getpid()),
    }
    process: subprocess.Popen[bytes] | None = None
    terminated = False

    def forward(signum: int, frame: object) -> None:
        # Ctrl-C reaches the server as a member of this process group; a
        # service manager signals this process alone.
        nonlocal terminated
        if signum == signal.SIGTERM:
            terminated = True
            if process is not None:
                process.terminate()

    previous = {
        sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        process = subprocess.Popen(_backend_command(), env=environment)
        if terminated:
            process.terminate()
        code = process.wait()
    except OSError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if code:
        # The server has said why; an exit asked for is a successful one.
        raise click.exceptions.Exit(code if code > 0 else 1)


@click.command()
@click.option(
    "--timeout",
    default=30,
    type=click.IntRange(min=0),
    help="Seconds to wait at each stop stage",
)
@click.option(
    "--force",
    is_flag=True,
    help="Cancel in-flight work after timeout, then force terminate as a last resort",
)
def stop(timeout: int, force: bool) -> None:
    """Stop the server through its API, after its in-flight work, and wait
    for it to exit."""
    locked, metadata = inspect_backend_lock()
    if not locked:
        click.echo(t("runtime.service_lock.not_running"))
        return
    if metadata is None:
        raise click.ClickException(t("runtime.service_lock.invalid_metadata"))
    pid = metadata.pid
    if not pid_exists(pid):
        raise click.ClickException(t("runtime.service_lock.missing_process", pid=pid))
    for cancel in [False, True] if force else [False]:
        try:
            _call("/shutdown", payload={"force": cancel}, timeout=5)
        except (BackendUnavailableError, httpx.HTTPError) as exc:
            if pid_exists(pid):
                raise click.ClickException(str(exc)) from exc
        if _wait_for_process_exit(pid, timeout):
            key = "stopped_after_cancel" if cancel else "stopped"
            click.echo(t(f"runtime.service_lock.{key}"))
            return
    if not force:
        click.echo(t("runtime.service_lock.stop_timeout"))
        return
    try:
        force_terminate_pid(pid)
    except OSError as exc:
        raise click.ClickException(
            t("runtime.service_lock.force_kill_failed", pid=pid, error=exc)
        ) from exc
    click.echo(t("runtime.service_lock.force_killed"))


def _wait_for_process_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while pid_exists(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)
    return True


@click.command()
@click.pass_context
def kill(ctx: click.Context) -> None:
    """Stop the server, cancelling its in-flight work, and force terminate it
    if needed.

    Equivalent to: `guildbotics stop --force --timeout 0`.
    """
    ctx.invoke(stop, timeout=0, force=True)
