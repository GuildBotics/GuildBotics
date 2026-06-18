from __future__ import annotations

import asyncio
import errno
import os
import signal
import sys
import time
import traceback
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

import click

from guildbotics.cli.member import member
from guildbotics.cli.workspace import workspace
from guildbotics.drivers import (
    CommandError,
    EventListenerRunner,
    PersonNotFoundError,
    PersonSelectionRequiredError,
    TaskScheduler,
    run_command,
)
from guildbotics.editions import get_edition
from guildbotics.utils.env_loader import (
    load_guildbotics_env,
    resolve_guildbotics_env_file,
)
from guildbotics.utils.fileio import (
    GUILDBOTICS_DATA_DIR,
    apply_workspace_data_root,
    get_machine_state_path,
)
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


def _pid_file_path() -> Path:
    return get_machine_state_path("run", "scheduler.pid")


def _pid_is_running(pid: int) -> bool:
    try:
        # Signal 0 checks for existence without sending a signal
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    else:
        return True


def _read_pidfile(path: Path) -> int | None:
    try:
        txt = path.read_text().strip()
        return int(txt) if txt else None
    except Exception:
        return None


def _write_pidfile(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid))


def _remove_pidfile(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


@click.group()
@click.version_option(
    version=_resolve_version(),
    prog_name="guildbotics",
    message="%(prog)s %(version)s",
)
def main() -> None:
    """GuildBotics CLI entrypoint."""
    pass


main.add_command(member)
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
    show_default=True,
    help="Stop a worker after this many consecutive workflow errors.",
)
@click.argument("default_routine_commands", nargs=-1)
def start(
    only_target: str | None,
    max_consecutive_errors: int,
    default_routine_commands: tuple[str, ...],
) -> None:
    """Start GuildBotics runtimes (scheduler and event listener runner)."""
    _load_env_from_cwd()
    pid_path = _pid_file_path()
    # Prevent multiple instances
    if pid_path.exists():
        old_pid = _read_pidfile(pid_path)
        if old_pid and _pid_is_running(old_pid):
            click.echo(
                f"Scheduler already running with PID {old_pid} (pidfile: {pid_path})."
            )
            return
        else:
            # Stale pidfile
            _remove_pidfile(pid_path)

    _write_pidfile(pid_path, os.getpid())

    edition = get_edition()

    routine_commands = list(default_routine_commands)
    if not routine_commands:
        routine_commands = edition.get_default_routines()

    start_scheduler = only_target in (None, "scheduler")
    start_events = only_target in (None, "events")

    scheduler = (
        TaskScheduler(
            edition.get_context(),
            routine_commands,
            consecutive_error_limit=max_consecutive_errors,
        )
        if start_scheduler
        else None
    )
    event_runner = EventListenerRunner(edition.get_context()) if start_events else None

    def _handle_signal(signum, frame):  # type: ignore[no-untyped-def]
        click.echo(f"Received signal {signum}. Shutting down...")
        try:
            if event_runner is not None:
                event_runner.stop()
            if scheduler is not None:
                scheduler.shutdown(graceful=True)
        finally:
            _remove_pidfile(pid_path)

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        if event_runner is not None:
            event_runner.start()
        if scheduler is not None:
            scheduler.start()
        elif event_runner is not None:
            while event_runner.is_alive():
                time.sleep(0.5)
    except KeyboardInterrupt:
        _handle_signal(signal.SIGINT, None)  # type: ignore[arg-type]
    finally:
        if event_runner is not None:
            event_runner.stop()
            event_runner.join(timeout=5.0)
        _remove_pidfile(pid_path)


@main.command()
@click.option(
    "--person",
    "person_option",
    help="Person ID or name to run the custom command as.",
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
@click.option("--timeout", default=30, show_default=True, help="Seconds to wait")
@click.option("--force", is_flag=True, help="Force kill after timeout")
def stop(timeout: int, force: bool) -> None:
    """Gracefully stop the running scheduler process."""
    _load_env_from_cwd()
    pid_path = _pid_file_path()

    if not pid_path.exists():
        click.echo("No pidfile found. Is the scheduler running?")
        return

    pid = _read_pidfile(pid_path)
    if not pid:
        click.echo(f"Invalid pidfile: {pid_path}")
        _remove_pidfile(pid_path)
        return

    if not _pid_is_running(pid):
        click.echo(f"Process {pid} is not running. Cleaning up pidfile.")
        _remove_pidfile(pid_path)
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        click.echo(f"Permission denied to signal process {pid}.")
        return
    except ProcessLookupError:
        click.echo(f"Process {pid} does not exist. Cleaning up pidfile.")
        _remove_pidfile(pid_path)
        return

    # Wait for graceful shutdown
    deadline = time.time() + max(0, timeout)
    while time.time() < deadline:
        if not _pid_is_running(pid):
            click.echo("Scheduler stopped.")
            _remove_pidfile(pid_path)
            return
        time.sleep(0.5)

    if force and _pid_is_running(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception as e:
            click.echo(f"Failed to SIGKILL {pid}: {e}")
        else:
            click.echo("Force killed scheduler.")
        # Best effort cleanup
        if not _pid_is_running(pid):
            _remove_pidfile(pid_path)
    else:
        click.echo("Timeout reached and process still running. Use --force to SIGKILL.")


@main.command()
@click.pass_context
def kill(ctx: click.Context) -> None:
    """Immediately force kill the running scheduler.

    Equivalent to: `guildbotics stop --force --timeout 0`.
    """
    ctx.invoke(stop, timeout=0, force=True)
