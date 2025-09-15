from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

import click
from dotenv import load_dotenv

from guildbotics.cli.setup_tool import SetupTool
from guildbotics.drivers import TaskScheduler
from guildbotics.utils.import_utils import instantiate_class


def get_setup_tool() -> SetupTool:
    name = os.getenv("GUILDBOTICS_EDITION", "simple")
    if "." not in name:
        name = f"guildbotics.cli.{name}.{name}_setup_tool.{name.capitalize()}SetupTool"
    return instantiate_class(name, expected_type=SetupTool)


def _resolve_version() -> str:
    try:
        return pkg_version("guildbotics")
    except PackageNotFoundError:
        try:
            from guildbotics._version import __version__ as v  # type: ignore

            return v
        except Exception:
            return "0.0.0+unknown"


def _load_env_from_cwd() -> None:
    dotenv_path = Path.cwd() / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path, override=False)


@click.group()
@click.version_option(
    version=_resolve_version(),
    prog_name="guildbotics",
    message="%(prog)s %(version)s",
)
def main() -> None:
    """GuildBotics CLI entrypoint."""
    pass


@main.command()
def run() -> None:
    """Run the GuildBotics application."""
    _load_env_from_cwd()
    TaskScheduler(get_setup_tool().get_context()).start()


@main.command()
def add() -> None:
    """Add a new member to the GuildBotics project."""
    _load_env_from_cwd()
    get_setup_tool().add_member()


@main.command()
def init() -> None:
    """Initialize the GuildBotics environment.

    This function sets up the necessary environment for GuildBotics to run.
    """
    _load_env_from_cwd()
    get_setup_tool().init_project()


@main.command()
def verify() -> None:
    """Verify the GuildBotics environment.

    This function checks the necessary environment for GuildBotics to run.
    """
    _load_env_from_cwd()
    get_setup_tool().verify_environment()


@main.command(name="version")
def version_cmd() -> None:
    """Print version."""
    click.echo(_resolve_version())
