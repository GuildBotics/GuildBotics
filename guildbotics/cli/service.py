"""Run the server of this checkout and its services, and end them."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Any

import click
import httpx

from guildbotics.drivers.app_api_lock import (
    KILL_AFTER_SECONDS,
    PARENT_PID_ENV,
    START_SERVICES_ENV,
    BackendMetadata,
    inspect_backend_lock,
)
from guildbotics.utils.i18n_tool import get_language, t
from guildbotics.utils.local_api import connect_local_api
from guildbotics.utils.processes import force_terminate_pid, pid_exists

POLL_SECONDS = 0.1


class BackendUnavailableError(RuntimeError):
    pass


def _call(
    path: str,
    *,
    port: int,
    payload: dict[str, Any] | None = None,
    timeout: float | None,
) -> None:
    with connect_local_api(
        headers={"Accept-Language": get_language()}, timeout=timeout, port=port
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


def _api_error(exc: Exception) -> click.ClickException:
    return click.ClickException(str(exc))


def _wait_for_process_exit(pid: int, timeout: float | None) -> bool:
    deadline = None if timeout is None else time.monotonic() + timeout
    while pid_exists(pid):
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)
    return True


def _running_server() -> BackendMetadata | None:
    """The server holding this machine's lock: its PID and port."""
    locked, metadata = inspect_backend_lock()
    if locked and metadata is None:
        raise click.ClickException(t("runtime.service_lock.invalid_metadata"))
    return metadata


@click.group()
def server() -> None:
    """Run and stop the server: the screens, the API, and the services in it."""


@server.command("start")
@click.option(
    "--services",
    is_flag=True,
    help="Also start the services, whatever the Service screen's setting says",
)
def start_server(services: bool) -> None:
    """Run the server in the foreground.

    It opens the selected workspace, or none until one is selected on the
    screens. It starts the services as well with --services, or when the
    Service screen's setting says so. It runs until `guildbotics server stop`.
    Ctrl-C asks the same: the first lets in-flight work finish, the second
    cancels it. SIGTERM, which an OS sends to stop its service, cancels it at
    once.
    """
    # The server resolves the workspace itself, as every launcher leaves it.
    environment = {**os.environ, PARENT_PID_ENV: str(os.getpid())}
    if services:
        environment[START_SERVICES_ENV] = "1"
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


@server.command("stop")
@click.option(
    "--force",
    is_flag=True,
    help="Cancel in-flight work instead of waiting for it, and force terminate "
    "the server if its teardown does not end",
)
def stop_server(force: bool) -> None:
    """Stop the server and wait for it to exit, after its in-flight work."""
    metadata = _running_server()
    if metadata is None:
        click.echo(t("runtime.service_lock.not_running"))
        return
    pid = metadata.pid
    if not pid_exists(pid):
        raise click.ClickException(t("runtime.service_lock.missing_process", pid=pid))
    try:
        _call("/shutdown", port=metadata.port, payload={"force": force}, timeout=5)
    except (BackendUnavailableError, httpx.HTTPError) as exc:
        if pid_exists(pid):
            raise _api_error(exc) from exc
    if not force:
        if not _wait_for_process_exit(pid, 2):
            click.echo(t("runtime.service_lock.waiting_for_work"))
            _wait_for_process_exit(pid, None)
        click.echo(t("runtime.service_lock.stopped"))
        return
    if _wait_for_process_exit(pid, KILL_AFTER_SECONDS):
        click.echo(t("runtime.service_lock.stopped_after_cancel"))
        return
    try:
        force_terminate_pid(pid)
    except OSError as exc:
        raise click.ClickException(
            t("runtime.service_lock.force_kill_failed", pid=pid, error=exc)
        ) from exc
    click.echo(t("runtime.service_lock.force_killed"))


@click.group()
def service() -> None:
    """Start and stop the services of a running server, as the Service screen
    does: scheduled commands, ticket patrol, and chat events."""


def _running_port() -> int:
    """The running server's port, as it recorded it in this machine's lock."""
    metadata = _running_server()
    if metadata is None:
        raise click.ClickException(t("runtime.service_lock.not_running"))
    return metadata.port


@service.command("start")
def start_service() -> None:
    """Start the services with the settings saved on the Service screen."""
    port = _running_port()
    try:
        _call("/scheduler/start", port=port, timeout=None)
    except (BackendUnavailableError, httpx.HTTPError) as exc:
        raise _api_error(exc) from exc
    click.echo(t("runtime.service_lock.services_started"))


@service.command("stop")
@click.option(
    "--force", is_flag=True, help="Cancel in-flight work instead of waiting for it"
)
def stop_service(force: bool) -> None:
    """Stop the services after their in-flight work; the server keeps running."""
    port = _running_port()
    try:
        _call("/scheduler/stop", port=port, payload={"force": force}, timeout=None)
    except (BackendUnavailableError, httpx.HTTPError) as exc:
        raise _api_error(exc) from exc
    click.echo(t("runtime.service_lock.services_stopped"))
