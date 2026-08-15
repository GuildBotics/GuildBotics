"""Hub commands, run on the machine that hosts the hub.

These are the commands a device invokes over SSH when it registers a workspace
with a hub it is not running on. They deliberately need no workspace: a hub is
machine-wide, and the SSH session that carries them has no selected workspace
of its own.
"""

from __future__ import annotations

import json
from typing import Any

import click

from guildbotics.cli._options import format_option
from guildbotics.hub import host

_format_option = format_option("markdown")


@click.group()
def hub() -> None:
    """Host a synchronization hub for GuildBotics workspaces on this machine."""


@hub.command(name="create")
@_format_option
def create_hub_command(output_format: str) -> None:
    """Make this machine a hub, or show the hub it already hosts."""
    _print(_hub_payload(host.create_hub()), output_format)


@hub.command(name="status")
@_format_option
def hub_status(output_format: str) -> None:
    """Show whether this machine hosts a hub, and which workspaces it holds."""
    settings = host.read_hub()
    if settings is None:
        _print({"hosted": False, "hub_root": str(host.hub_root())}, output_format)
        return
    _print(_hub_payload(settings), output_format)


@hub.group(name="workspace")
def hub_workspace() -> None:
    """Manage the repositories this hub holds, one per workspace."""


@hub_workspace.command(name="create")
@click.argument("workspace_id")
@_format_option
def create_hub_workspace(workspace_id: str, output_format: str) -> None:
    """Create the repository a workspace synchronizes through."""
    try:
        path = host.create_workspace_repository(workspace_id)
    except (ValueError, host.HubNotHostedError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    _print({"workspace_id": workspace_id, "repository": str(path)}, output_format)


@hub_workspace.command(name="list")
@_format_option
def list_hub_workspaces(output_format: str) -> None:
    """List the workspaces this hub holds."""
    _print({"workspaces": host.list_workspace_ids()}, output_format)


def _hub_payload(settings: host.HubSettings) -> dict[str, Any]:
    return {
        "hosted": True,
        "hub_root": str(host.hub_root()),
        "hub_id": settings.hub_id,
        "created_at": settings.created_at,
        "ssh_endpoint": settings.ssh_endpoint,
        "workspaces": host.list_workspace_ids(),
    }


def _print(payload: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if not payload.get("hosted", True):
        click.echo(
            "\n".join(
                [
                    "Hub: not hosted on this machine",
                    f"Hub root: {payload['hub_root']}",
                ]
            )
        )
        return
    click.echo(
        "\n".join(
            f"{_LABELS.get(key, key)}: {_display(value)}"
            for key, value in payload.items()
        )
    )


def _display(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "(none)"
    return str(value)


_LABELS = {
    "hosted": "Hub hosted",
    "hub_root": "Hub root",
    "hub_id": "Hub ID",
    "created_at": "Created at",
    "ssh_endpoint": "SSH endpoint",
    "workspaces": "Workspaces",
    "workspace_id": "Workspace ID",
    "repository": "Repository",
}
