"""Launch and control the same backend that Desktop runs."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from functools import partial
from pathlib import Path
from typing import Any

import click
import httpx

from guildbotics.cli._options import selected_workspace
from guildbotics.drivers.app_api_lock import (
    CONFLICT_NOTICE_PREFIX,
    OWNER_ENV,
    PORT_NOTICE_PREFIX,
    inspect_backend_lock,
    read_backend_metadata,
)
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.local_api import connect_local_api, server_port
from guildbotics.utils.processes import force_terminate_pid, pid_exists

POLL_SECONDS = 0.1
STARTUP_TIMEOUT = 45.0
STOP_STAGES = 2


class BackendUnavailableError(RuntimeError):
    pass


class StartupInterruptedError(Exception):
    pass


def _call(
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float | None = None,
    method: str = "POST",
) -> dict[str, Any]:
    with connect_local_api(timeout=timeout) as client:
        if client is None:
            raise BackendUnavailableError(t("runtime.service_lock.api_unavailable"))
        response = client.request(method, path, json=payload)
        response.raise_for_status()
        return dict(response.json())


def _background(call: Callable[[], dict[str, Any]]) -> Future[dict[str, Any]]:
    result: Future[dict[str, Any]] = Future()

    def run() -> None:
        try:
            result.set_result(call())
        except Exception as exc:
            result.set_exception(exc)

    threading.Thread(target=run, daemon=True).start()
    return result


def _backend_command() -> list[str]:
    if getattr(sys, "frozen", False):
        suffix = ".exe" if os.name == "nt" else ""
        return [str(Path(sys.executable).with_name("guildbotics-app-api" + suffix))]
    return [sys.executable, "-m", "guildbotics.app_api"]


def _launch_backend(workspace: Path) -> tuple[subprocess.Popen[str], queue.Queue[str]]:
    environment = dict(os.environ)
    environment[GUILDBOTICS_WORKSPACE_ROOT] = str(workspace)
    environment[OWNER_ENV] = "cli"
    environment["GUILDBOTICS_APP_API_PARENT_PID"] = str(os.getpid())
    process = subprocess.Popen(
        _backend_command(),
        env=environment,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name != "nt",
        creationflags=int(vars(subprocess)["CREATE_NEW_PROCESS_GROUP"])
        if sys.platform == "win32"
        else 0,
    )
    notices: queue.Queue[str] = queue.Queue()

    def drain() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            if line.startswith((PORT_NOTICE_PREFIX, CONFLICT_NOTICE_PREFIX)):
                notices.put(line.strip())
            else:
                click.echo(line, nl=False, err=True)

    threading.Thread(target=drain, daemon=True).start()
    return process, notices


def _wait_for_port(
    process: subprocess.Popen[str],
    notices: queue.Queue[str],
    signals: list[int] | None = None,
) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if signals:
            raise StartupInterruptedError
        try:
            notice = notices.get(timeout=POLL_SECONDS)
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        if notice == f"{PORT_NOTICE_PREFIX}{server_port()}":
            return
        if notice.startswith(CONFLICT_NOTICE_PREFIX):
            raise click.ClickException(
                _conflict_message(notice.removeprefix(CONFLICT_NOTICE_PREFIX))
            )
        break
    raise click.ClickException(t("runtime.service_lock.start_failed"))


def _conflict_message(owner: str) -> str:
    key = "desktop_start" if owner == "desktop" else "already_running"
    return t(f"runtime.service_lock.{key}")


def _stop_requested(pid: int) -> bool:
    metadata = read_backend_metadata()
    return metadata is not None and metadata.pid == pid and metadata.stop_requested


def _start_service(
    process: subprocess.Popen[str], payload: dict[str, Any]
) -> dict[str, Any]:
    # The notice precedes workspace/keychain loading. User interaction is not
    # counted against the bind deadline.
    while process.poll() is None:
        try:
            return _call("/scheduler/start", payload=payload)
        except BackendUnavailableError:
            time.sleep(POLL_SECONDS)
    raise BackendUnavailableError(t("runtime.service_lock.start_failed"))


def _supervise(
    process: subprocess.Popen[str], payload: dict[str, Any], signals: list[int]
) -> None:
    starting = _background(lambda: _start_service(process, payload))
    while not starting.done():
        if len(signals) >= STOP_STAGES:
            # Startup can be waiting on synchronization or a keychain prompt.
            # No successful service start to drain yet; the second interrupt
            # must still be able to end this attempt.
            process.kill()
            process.wait()
            return
        if process.poll() is not None:
            raise BackendUnavailableError(t("runtime.service_lock.start_failed"))
        time.sleep(POLL_SECONDS)
    status = starting.result()
    sent = 0
    stopping: list[Future[dict[str, Any]]] = []
    while True:
        # A handler never performs I/O or waits, so a second signal can
        # escalate while the graceful API request is blocked.
        if len(signals) > sent and sent < STOP_STAGES:
            force = sent > 0
            sent = 2 if force else 1
            stopping.append(
                _background(partial(_call, "/scheduler/stop", payload={"force": force}))
            )
        intentional = bool(signals) or _stop_requested(process.pid)
        if process.poll() is not None:
            if intentional:
                return
            raise BackendUnavailableError(t("runtime.service_lock.service_failed"))
        for request in stopping:
            if request.done():
                request.result()
        if sent == STOP_STAGES and stopping[-1].done():
            # A force stop has its own bounded waits and may report a unit
            # that could not stop. Finish by shutting down the backend rather
            # than polling that failed unit forever.
            return
        units = [status["scheduler"], status["events"]]
        if not any(unit["running"] for unit in units) and not status.get(
            "has_active_work", False
        ):
            if intentional:
                return
            raise BackendUnavailableError(t("runtime.service_lock.service_failed"))
        if any(unit["state"] == "failed" for unit in units) and not intentional:
            raise BackendUnavailableError(t("runtime.service_lock.service_failed"))
        time.sleep(POLL_SECONDS)
        try:
            status = _call("/scheduler/status", timeout=5, method="GET")
        except BackendUnavailableError, httpx.HTTPError:
            if process.poll() is not None and (
                bool(signals) or _stop_requested(process.pid)
            ):
                return
            raise


def _close_backend(process: subprocess.Popen[str], *, announced: bool) -> None:
    if process.poll() is None:
        budget = 5.0
        if announced:
            try:
                reply = _call("/shutdown", timeout=5)
                budget += float(reply["teardown_budget_seconds"])
            except BackendUnavailableError, httpx.HTTPError:
                process.terminate()
        else:
            process.terminate()
        try:
            process.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if process.stdout is not None:
        process.stdout.close()


@click.command()
@click.option(
    "--only",
    "only_target",
    type=click.Choice(["scheduler", "events"], case_sensitive=False),
    default=None,
    help="Start only scheduled/routine work or event reception and work.",
)
@click.option(
    "--max-consecutive-errors",
    type=click.IntRange(min=1),
    default=3,
    help="Stop a worker after this many consecutive workflow errors.",
)
def start(only_target: str | None, max_consecutive_errors: int) -> None:
    """Start the backend and its services; Desktop must be closed."""
    workspace = selected_workspace()
    locked, metadata = inspect_backend_lock()
    if locked:
        raise click.ClickException(
            _conflict_message(metadata.owner if metadata else "unknown")
        )
    signals: list[int] = []

    def handle(signum: int, frame: object) -> None:
        signals.append(signum)

    previous = {
        sig: signal.signal(sig, handle) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    process: subprocess.Popen[str] | None = None
    announced = False
    try:
        process, notices = _launch_backend(workspace)
        _wait_for_port(process, notices, signals)
        announced = True
        _supervise(
            process,
            {
                "sources": {
                    "scheduled": only_target != "events",
                    "routine": only_target != "events",
                    "event_queue": only_target != "scheduler",
                },
                "max_consecutive_errors": max_consecutive_errors,
            },
            signals,
        )
    except StartupInterruptedError:
        return
    except (OSError, BackendUnavailableError, httpx.HTTPError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        try:
            if process is not None:
                _close_backend(process, announced=announced)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


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
    """Stop CLI services through the local backend API and wait for its exit."""
    locked, metadata = inspect_backend_lock()
    if not locked:
        click.echo(t("runtime.service_lock.not_running"))
        return
    if metadata is None:
        raise click.ClickException(t("runtime.service_lock.invalid_metadata"))
    if metadata.owner == "desktop":
        raise click.ClickException(t("runtime.service_lock.desktop_managed"))
    pid = metadata.pid
    if not pid_exists(pid):
        raise click.ClickException(t("runtime.service_lock.missing_process", pid=pid))
    for cancel in [False, True] if force else [False]:
        try:
            _call(
                "/scheduler/stop", payload={"force": cancel, "wait": False}, timeout=5
            )
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
    """Cancel CLI services and force terminate the backend if needed.

    Equivalent to: `guildbotics stop --force --timeout 0`.
    """
    ctx.invoke(stop, timeout=0, force=True)
