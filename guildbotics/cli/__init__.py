from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time
import traceback
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

import click

from guildbotics.cli.diagnostics import diagnostics
from guildbotics.cli.member import member
from guildbotics.cli.secrets import secrets
from guildbotics.cli.workspace import workspace
from guildbotics.drivers import (
    CommandError,
    EventListenerRunner,
    PersonExecutionNotAllowedError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
    TaskScheduler,
    run_command,
)
from guildbotics.editions import get_edition
from guildbotics.observability import new_id
from guildbotics.observability.diagnostics_events import (
    finish_system_session,
    install_diagnostics_log_handler,
    start_system_session,
)
from guildbotics.runtime.service_control import (
    ServiceControlWatcher,
    StopStage,
    clear_stop_request,
    write_stop_request,
)
from guildbotics.runtime.service_lock import (
    ServiceLock,
    ServiceLockMetadata,
    ServiceLockUnavailableError,
    inspect_service_lock,
)
from guildbotics.utils.env_loader import (
    load_guildbotics_env,
    resolve_guildbotics_env_file,
)
from guildbotics.utils.fileio import (
    GUILDBOTICS_DATA_DIR,
    apply_workspace_data_root,
    get_machine_state_path,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.log_utils import get_logger
from guildbotics.utils.processes import force_terminate_pid, pid_exists
from guildbotics.utils.workspace_state import GUILDBOTICS_CONFIG_DIR


def _resolve_version() -> str:
    try:
        return pkg_version("guildbotics")
    except PackageNotFoundError:
        try:
            from guildbotics._version import __version__ as v  # type: ignore

            return v
        except Exception:
            return "0.0.0+unknown"


def _apply_runtime_workspace(workspace_root: Path) -> Path:
    root = workspace_root.expanduser().resolve(strict=False)
    env_file = resolve_guildbotics_env_file(root, prefer_env_file=True)
    inherited_data_dir = os.getenv(GUILDBOTICS_DATA_DIR, "").strip() or None
    apply_workspace_data_root(
        root,
        env_file,
        inherited_data_dir=inherited_data_dir,
    )
    load_guildbotics_env(root, override=False, prefer_env_file=True)
    if not os.getenv(GUILDBOTICS_CONFIG_DIR, "").strip():
        os.environ[GUILDBOTICS_CONFIG_DIR] = str(root / ".guildbotics" / "config")
    return root


def _load_env_from_cwd() -> None:
    _apply_runtime_workspace(Path.cwd())


def _service_lock_path() -> Path:
    return get_machine_state_path("run", "service.lock")


def _stop_request_path() -> Path:
    return get_machine_state_path("run", "stop-request.json")


@click.group(context_settings={"show_default": True})
@click.version_option(
    version=_resolve_version(),
    prog_name="guildbotics",
    message="%(prog)s %(version)s",
)
def main() -> None:
    """GuildBotics CLI entrypoint."""
    install_diagnostics_log_handler(get_logger())


main.add_command(diagnostics)
main.add_command(member)
main.add_command(secrets)
main.add_command(workspace)


@main.command()
@click.option(
    "--only",
    "only_target",
    type=click.Choice(["scheduler", "events"], case_sensitive=False),
    default=None,
    help="Start only one runtime instead of both scheduler and event listener runner.",
)
@click.option(
    "--max-consecutive-errors",
    type=int,
    default=3,
    help="Stop a worker after this many consecutive workflow errors.",
)
def start(
    only_target: str | None,
    max_consecutive_errors: int,
) -> None:
    """Start GuildBotics runtimes (scheduler and event listener runner)."""
    _load_env_from_cwd()
    request_path = _stop_request_path()
    service_lock = ServiceLock(_service_lock_path())
    try:
        metadata = service_lock.acquire(
            owner="cli",
            workspace=Path.cwd(),
            before_publish=lambda: clear_stop_request(request_path),
        )
    except ServiceLockUnavailableError as exc:
        raise click.ClickException(
            _service_lock_conflict_message(exc.metadata)
        ) from exc

    try:
        _run_cli_background_service(
            only_target=only_target,
            max_consecutive_errors=max_consecutive_errors,
            service_instance_id=metadata.service_instance_id,
            request_path=request_path,
        )
    finally:
        clear_stop_request(request_path)
        service_lock.release()


def _run_cli_background_service(
    *,
    only_target: str | None,
    max_consecutive_errors: int,
    service_instance_id: str,
    request_path: Path,
) -> None:
    start_system_session(new_id())
    try:
        _run_cli_background_service_session(
            only_target=only_target,
            max_consecutive_errors=max_consecutive_errors,
            service_instance_id=service_instance_id,
            request_path=request_path,
        )
    finally:
        finish_system_session()


def _run_cli_background_service_session(
    *,
    only_target: str | None,
    max_consecutive_errors: int,
    service_instance_id: str,
    request_path: Path,
) -> None:
    edition = get_edition()

    scheduler_sources_enabled = only_target in (None, "scheduler")
    start_events = only_target in (None, "events")
    start_member_worker = scheduler_sources_enabled or start_events

    scheduler = (
        TaskScheduler(
            edition.get_context(),
            consecutive_error_limit=max_consecutive_errors,
            scheduled_source_enabled=scheduler_sources_enabled,
            routine_source_enabled=scheduler_sources_enabled,
            event_queue_source_enabled=start_events,
        )
        if start_member_worker
        else None
    )
    event_runner = EventListenerRunner(edition.get_context()) if start_events else None

    graceful_stop_started = threading.Event()

    def _request_shutdown(*, cancel: bool) -> None:
        # Signal-handler safe: only sets flags / schedules non-blocking stops
        # and never joins, so the handler returns immediately and a second
        # signal can still be delivered to escalate the stop.
        if event_runner is not None:
            event_runner.stop()
        if scheduler is not None:
            scheduler.request_shutdown(graceful=not cancel)

    def _handle_signal(signum, frame):  # type: ignore[no-untyped-def]
        if graceful_stop_started.is_set():
            # Second signal: escalate like the GUI force stop and cancel the
            # in-flight work the graceful stop is still waiting on. The main
            # thread does the joining, so this handler must not block.
            click.echo("Cancelling in-flight work...")
            _request_shutdown(cancel=True)
            return
        graceful_stop_started.set()
        click.echo(
            f"Received signal {signum}. Waiting for in-flight work to finish "
            "(send the signal again to cancel it)..."
        )
        _request_shutdown(cancel=False)

    watcher = ServiceControlWatcher(
        service_instance_id,
        _request_shutdown,
        path=request_path,
    )

    # Signals are reserved for foreground Ctrl+C. Background stop requests use
    # the cross-platform service-control file watched above.
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        watcher.start()
        if event_runner is not None:
            event_runner.start()
        if scheduler is not None:
            # Blocks until workers exit; the signal handler above only requests
            # the stop, so the join that waits it out happens here in the main
            # thread and remains interruptible by a second (escalating) signal.
            scheduler.start()
        if event_runner is not None:
            _wait_for_event_runner(event_runner)
    except KeyboardInterrupt:
        _request_shutdown(cancel=False)
    finally:
        watcher.close()
        if scheduler is not None:
            scheduler.shutdown(graceful=True)
        if event_runner is not None:
            event_runner.stop()
            event_runner.join(timeout=5.0)


def _wait_for_event_runner(event_runner: EventListenerRunner) -> None:
    while event_runner.is_alive():
        time.sleep(1.0)


@main.command()
@click.option(
    "--person",
    "person_option",
    help=(
        "Person ID or name to run the custom command as. Defaults to the team's "
        "default_person_id, else its first active non-human member in ID order."
    ),
)
@click.option(
    "--cwd",
    type=str,
    default=None,
    help="Specify the working directory for the custom command.",
)
@click.argument("custom_command", required=True)
@click.argument("command_args", nargs=-1)
def run(
    person_option: str | None,
    cwd: str | None,
    custom_command: str,
    command_args: tuple[str, ...],
) -> None:
    """Run the GuildBotics application."""
    runtime_workspace = _apply_runtime_workspace(Path(cwd) if cwd else Path.cwd())
    message = "" if sys.stdin.isatty() else sys.stdin.read()
    asyncio.run(
        _run_custom_command(
            custom_command,
            command_args,
            person_option,
            message,
            runtime_workspace if cwd else None,
        )
    )


async def _run_custom_command(
    command_spec: str,
    command_args: tuple[str, ...],
    person_option: str | None,
    message: str,
    cwd: Path | None = None,
) -> None:
    command_name, inline_person = _parse_command_spec(command_spec)
    edition = get_edition()
    context = edition.get_context(message)
    identifier = person_option or inline_person

    try:
        rendered = await run_command(
            context,
            command_name=command_name,
            command_args=command_args,
            person_identifier=identifier,
            cwd=cwd,
        )
    except PersonSelectionRequiredError as exc:
        available = ", ".join(exc.available) if exc.available else "none"
        raise click.ClickException(
            "Specify a person using '--person' or '<command>@person'."
            f" Available: {available}"
        ) from exc
    except PersonNotFoundError as exc:
        available = ", ".join(exc.available) if exc.available else "none"
        raise click.ClickException(
            f"Person '{exc.identifier}' not found. Available: {available}"
        ) from exc
    except PersonExecutionNotAllowedError as exc:
        raise click.ClickException(str(exc)) from exc
    except CommandError as exc:
        traceback.print_exc()
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive guard
        traceback.print_exc()
        raise click.ClickException(str(exc)) from exc

    if rendered:
        click.echo(rendered)


def _parse_command_spec(command_spec: str) -> tuple[str, str | None]:
    parts = command_spec.split("@", 1)
    name = parts[0].strip()
    if not name:
        raise click.ClickException("Command name cannot be empty.")
    person = parts[1].strip() if len(parts) > 1 else None
    if person == "":
        person = None
    return name, person


@main.command(name="version")
def version_cmd() -> None:
    """Print version."""
    click.echo(_resolve_version())


@main.command()
@click.option(
    "--timeout",
    default=30,
    help="Seconds to wait at each stop stage",
)
@click.option(
    "--force",
    is_flag=True,
    help="Cancel in-flight work after timeout, then force terminate as a last resort",
)
def stop(timeout: int, force: bool) -> None:
    """Gracefully stop a CLI-managed background service."""
    _load_env_from_cwd()
    status = inspect_service_lock(_service_lock_path())
    if not status.locked:
        click.echo(t("runtime.service_lock.not_running"))
        return
    metadata = status.metadata
    if metadata is None:
        raise click.ClickException(t("runtime.service_lock.invalid_metadata"))
    if metadata.owner == "desktop":
        raise click.ClickException(t("runtime.service_lock.desktop_managed"))

    pid = metadata.pid
    if not pid_exists(pid):
        raise click.ClickException(t("runtime.service_lock.missing_process", pid=pid))

    _write_service_stop_request(metadata, "graceful")

    # Wait for graceful shutdown (the scheduler finishes in-flight work first).
    if _wait_for_process_exit(pid, timeout):
        click.echo(t("runtime.service_lock.stopped"))
        return

    if not force:
        click.echo(t("runtime.service_lock.stop_timeout"))
        return

    # Escalate monotonically from graceful to cancellation, then force stop.
    _write_service_stop_request(metadata, "cancel")
    if _wait_for_process_exit(pid, timeout):
        click.echo(t("runtime.service_lock.stopped_after_cancel"))
        return

    try:
        force_terminate_pid(pid)
    except Exception as e:
        click.echo(t("runtime.service_lock.force_kill_failed", pid=pid, error=e))
    else:
        click.echo(t("runtime.service_lock.force_killed"))


def _write_service_stop_request(
    metadata: ServiceLockMetadata,
    stage: StopStage,
) -> None:
    try:
        write_stop_request(
            metadata.service_instance_id,
            stage,
            _stop_request_path(),
        )
    except PermissionError as exc:
        raise click.ClickException(
            t("runtime.service_lock.permission_denied", pid=metadata.pid)
        ) from exc
    except TimeoutError as exc:
        raise click.ClickException(
            t("runtime.service_lock.control_timeout", pid=metadata.pid)
        ) from exc


def _wait_for_process_exit(pid: int, timeout: float) -> bool:
    deadline = time.time() + max(0, timeout)
    while True:
        if not pid_exists(pid):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


@main.command()
@click.pass_context
def kill(ctx: click.Context) -> None:
    """Immediately force kill a CLI-managed background service.

    Equivalent to: `guildbotics stop --force --timeout 0`.
    """
    ctx.invoke(stop, timeout=0, force=True)


def _service_lock_conflict_message(metadata: ServiceLockMetadata | None) -> str:
    if metadata is None:
        return t("runtime.service_lock.already_running")
    return t(
        "runtime.service_lock.already_running_with_owner",
        owner=metadata.owner,
        pid=metadata.pid,
        workspace=metadata.workspace,
    )
