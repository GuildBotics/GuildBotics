"""The ``guildbotics environment`` commands: this device's isolated agent environment.

Building the snapshot the shared declaration asks for, logging in to an AI
CLI tool inside it, showing what this device holds, and removing it. A
device without a Desktop -- a headless Linux box that joined the workspace --
has only these; the Desktop shows the same state.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import click

from guildbotics.cli._options import (
    apply_workspace_option,
    format_option,
    workspace_option,
)
from guildbotics.intelligences.agent_environment import (
    provider_state,
    runtime,
    snapshot,
)
from guildbotics.intelligences.agent_environment.runtime import AgentEnvironmentError
from guildbotics.intelligences.agent_environment.toolchain import (
    ToolchainDeclaration,
    ToolchainError,
    load_toolchain,
    upstream_nameservers,
)
from guildbotics.intelligences.cli_agents import CLI_AGENTS, cli_agent_info

_PROVISIONED = [agent.name for agent in CLI_AGENTS if agent.provision.package]


@click.group()
@workspace_option
def environment(workspace_dir: Path | None) -> None:
    """Build and log in to the isolated agent environment on this device."""
    apply_workspace_option(workspace_dir)


@environment.command(name="build")
@click.option(
    "--force",
    is_flag=True,
    help="Rebuild even when the snapshot already matches the declaration.",
)
def build_command(force: bool) -> None:
    """Build the environment the shared declaration asks for.

    The build installs packages and nothing else, so it needs no input. A
    build that fails is remembered until the declaration changes or this
    command runs again.
    """
    _require_runtime()
    declaration = _declaration()
    status = snapshot.snapshot_status(declaration)
    if status.state == "ready" and not force:
        click.echo(f"The environment {status.name} is already up to date.")
        return
    if status.state == "building":
        raise click.ClickException("A build of this environment is already running.")
    try:
        built = asyncio.run(snapshot.build_snapshot(declaration, on_line=click.echo))
    except (AgentEnvironmentError, ToolchainError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"The environment {built.name} is ready.")


@environment.command(name="login")
@click.argument("tool", type=click.Choice(_PROVISIONED))
def login_command(tool: str) -> None:
    """Log in to an AI CLI tool inside the environment.

    The tool's own login command runs in the environment and talks you
    through it here; what it stores stays on this device, outside the
    snapshot, and every member uses it.
    """
    _require_runtime()
    info = cli_agent_info(tool)
    declaration = _declaration()
    status = snapshot.snapshot_status(declaration)
    if status.state != "ready":
        raise click.ClickException(
            f"The environment is {status.state}; build it first with "
            "`guildbotics environment build`."
        )
    try:
        code = asyncio.run(
            provider_state.login(
                info,
                declaration,
                snapshot=status.path,
                read_line=_read_stdin_line,
                write_line=click.echo,
            )
        )
    except (AgentEnvironmentError, ToolchainError) as exc:
        raise click.ClickException(str(exc)) from exc
    if code != 0:
        raise click.ClickException(f"{info.label} login exited with code {code}.")
    if not provider_state.is_logged_in(info):
        raise click.ClickException(
            f"{info.label} login finished but stored no credentials."
        )
    click.echo(f"{info.label} is logged in on this device.")


def _read_stdin_line() -> str | None:
    return sys.stdin.readline() or None


@environment.command(name="status")
@format_option("markdown")
def status_command(output_format: str) -> None:
    """Show the environment's runtime, snapshot, and logins on this device."""
    payload = _status_payload()
    if output_format == "json":
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    health = payload["runtime"]
    click.echo(
        f"runtime: {'available ' + health['version'] if health['available'] else 'unavailable: ' + health['reason']}"
    )
    state = payload["snapshot"]
    detail = f" ({state['detail']})" if state["detail"] else ""
    click.echo(f"snapshot: {state['state']} {state['name']}{detail}")
    click.echo(f"location: {state['path']}")
    dns = payload["dns"]
    click.echo(
        f"dns: {dns['declared']} -> {', '.join(dns['nameservers']) or dns['problem']}"
    )
    for tool in payload["tools"]:
        if not tool["provisioned"]:
            login = "not provisioned"
        elif tool["logged_in"]:
            login = "logged in"
        else:
            login = (
                f"not logged in (run `guildbotics environment login {tool['name']}`)"
            )
        click.echo(f"{tool['name']}: {login}")


def _status_payload() -> dict[str, Any]:
    health = runtime.doctor()
    dns_payload: dict[str, Any] = {"declared": "", "nameservers": [], "problem": ""}
    try:
        declaration = _declaration()
        state = snapshot.snapshot_status(declaration)
        snapshot_payload = {
            "state": state.state,
            "name": state.name,
            "path": str(state.path),
            "detail": state.detail,
        }
        declared = declaration.dns.nameservers
        dns_payload["declared"] = (
            declared if isinstance(declared, str) else ", ".join(declared)
        )
        try:
            dns_payload["nameservers"] = list(upstream_nameservers(declaration.dns))
        except ToolchainError as exc:
            dns_payload["problem"] = str(exc)
    except click.ClickException as exc:
        snapshot_payload = {
            "state": "missing",
            "name": "",
            "path": "",
            "detail": exc.message,
        }
    return {
        "runtime": {
            "available": health.available,
            "reason": health.reason,
            "version": health.runtime_version,
        },
        "snapshot": snapshot_payload,
        "dns": dns_payload,
        "tools": [
            {
                "name": agent.name,
                "label": agent.label,
                "provisioned": bool(agent.provision.package),
                "logged_in": provider_state.is_logged_in(agent),
            }
            for agent in CLI_AGENTS
        ],
    }


@environment.command(name="remove")
def remove_command() -> None:
    """Remove the workspace's snapshots from this device (logins are kept)."""
    _require_runtime()
    try:
        removed = asyncio.run(snapshot.remove_snapshots())
    except AgentEnvironmentError as exc:
        raise click.ClickException(str(exc)) from exc
    if not removed:
        click.echo("This device holds no snapshot of the workspace.")
        return
    for name in removed:
        click.echo(f"removed {name}")


def _require_runtime() -> None:
    health = runtime.doctor()
    if not health.available:
        raise click.ClickException(health.reason)


def _declaration() -> ToolchainDeclaration:
    try:
        return load_toolchain()
    except ToolchainError as exc:
        raise click.ClickException(str(exc)) from exc
