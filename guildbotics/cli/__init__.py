from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

import click

from guildbotics.cli._options import CLI_CONTEXT_SETTINGS, LazyGroup
from guildbotics.observability.diagnostics_events import (
    install_diagnostics_log_handler,
)
from guildbotics.utils.log_utils import get_logger


def _resolve_version() -> str:
    try:
        return pkg_version("guildbotics")
    except PackageNotFoundError:
        try:
            from guildbotics._version import __version__ as v  # type: ignore

            return v
        except Exception:
            return "0.0.0+unknown"


def _configure_windows_standard_streams() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


@click.group(
    cls=LazyGroup,
    context_settings=CLI_CONTEXT_SETTINGS,
    lazy_commands={
        "diagnostics": "guildbotics.cli.diagnostics:diagnostics",
        "environment": "guildbotics.cli.environment:environment",
        "hub": "guildbotics.cli.hub:hub",
        "kill": "guildbotics.cli.service:kill",
        "member": "guildbotics.cli.member:member",
        "run": "guildbotics.cli.run:run",
        "secrets": "guildbotics.cli.secrets:secrets",
        "start": "guildbotics.cli.service:start",
        "stop": "guildbotics.cli.service:stop",
        "workspace": "guildbotics.cli.workspace:workspace",
    },
)
@click.version_option(
    version=_resolve_version(),
    prog_name="guildbotics",
    message="%(prog)s %(version)s",
)
def main() -> None:
    """GuildBotics CLI entrypoint."""
    _configure_windows_standard_streams()
    install_diagnostics_log_handler(get_logger())


@main.command(name="version")
def version_cmd() -> None:
    """Print version."""
    click.echo(_resolve_version())
