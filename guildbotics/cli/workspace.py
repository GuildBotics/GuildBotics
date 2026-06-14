from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from guildbotics.utils.workspace_state import (
    read_active_workspace,
    workspace_status_payload,
    write_active_workspace,
)

FormatChoice = click.Choice(["json", "markdown"])


@click.group()
def workspace() -> None:
    """Manage the active GuildBotics workspace used by CLI agents."""


@workspace.command(name="use")
@click.argument(
    "workspace_dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
)
@click.option("--format", "output_format", type=FormatChoice, default="markdown")
def use_workspace(workspace_dir: Path, output_format: str) -> None:
    """Persist the active workspace for desktop and external CLI agents."""
    try:
        state = write_active_workspace(workspace_dir)
    except NotADirectoryError as exc:
        raise click.ClickException(f"workspace does not exist: {exc}") from exc
    _print(workspace_status_payload(state), output_format)


@workspace.command(name="current")
@click.option("--format", "output_format", type=FormatChoice, default="markdown")
def current_workspace(output_format: str) -> None:
    """Show the persisted active workspace."""
    state = read_active_workspace()
    if state is None:
        raise click.ClickException(
            "No active GuildBotics workspace is configured. "
            "Run `guildbotics workspace use <path>` or select a workspace in "
            "GuildBotics desktop."
        )
    _print(workspace_status_payload(state), output_format)


@workspace.command(name="status")
@click.option("--format", "output_format", type=FormatChoice, default="markdown")
def workspace_status(output_format: str) -> None:
    """Show active workspace status without failing when it is missing."""
    _print(workspace_status_payload(), output_format)


def _print(payload: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if not payload.get("configured"):
        click.echo(
            f"Active workspace: not configured\nState file: {payload['state_file']}"
        )
        return
    click.echo(
        "\n".join(
            [
                f"Active workspace: {payload['workspace']}",
                f"Config dir: {payload['config_dir']}",
                f"Config dir exists: {payload['config_dir_exists']}",
                f"Env file: {payload['env_file']}",
                f"Env file exists: {payload['env_file_exists']}",
                f"State file: {payload['state_file']}",
            ]
        )
    )
