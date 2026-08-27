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
from guildbotics.hub import host, relay, secret_host, secret_stream
from guildbotics.utils.keychain import InvalidSecretKeyError, SecretStoreError
from guildbotics.utils.secret_store import is_locked_error, keyring_status

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


@hub.group(name="owner")
def hub_owner() -> None:
    """Inspect or explicitly change a workspace's service owner."""


@hub_owner.command(name="get")
@click.argument("workspace_id")
@_format_option
def get_service_owner(workspace_id: str, output_format: str) -> None:
    """Show the current service owner, if one has been claimed."""
    try:
        owner = relay.read_service_owner(workspace_id)
    except (ValueError, host.HubError, relay.HubRelayError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    _print(
        {"workspace_id": workspace_id, "owner": _owner_payload(owner)},
        output_format,
    )


@hub_owner.command(name="claim")
@click.argument("workspace_id")
@click.argument("device_id")
@_format_option
def claim_service_owner(workspace_id: str, device_id: str, output_format: str) -> None:
    """Claim ownership only when the workspace has no owner."""
    try:
        owner, claimed = relay.claim_service_owner(workspace_id, device_id)
    except (ValueError, host.HubError, relay.HubRelayError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    _print(
        {
            "workspace_id": workspace_id,
            "claimed": claimed,
            "owner": _owner_payload(owner),
        },
        output_format,
    )


@hub_owner.command(name="transfer")
@click.argument("workspace_id")
@click.argument("device_id")
@_format_option
def transfer_service_owner(
    workspace_id: str, device_id: str, output_format: str
) -> None:
    """Move ownership explicitly to another device."""
    try:
        owner = relay.transfer_service_owner(workspace_id, device_id)
    except (ValueError, host.HubError, relay.HubRelayError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    _print(
        {"workspace_id": workspace_id, "owner": _owner_payload(owner)},
        output_format,
    )


@hub.group(name="live")
def hub_live() -> None:
    """Publish and watch transient workspace live state."""


@hub_live.command(name="publish")
@click.argument("workspace_id")
@click.argument("device_id")
@click.argument("publisher_id")
def publish_live(workspace_id: str, device_id: str, publisher_id: str) -> None:
    """Replace one live snapshot for each non-empty stdin line."""
    try:
        relay.publish_live_lines(
            workspace_id,
            device_id,
            publisher_id,
            click.get_text_stream("stdin"),
        )
    except (ValueError, host.HubError, relay.HubRelayError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@hub_live.command(name="watch")
@click.argument("workspace_id")
def watch_live(workspace_id: str) -> None:
    """Poll live snapshots and head updates until the SSH connection closes."""
    try:
        relay.watch_live(workspace_id, output=click.get_text_stream("stdout"))
    except (ValueError, host.HubError, relay.HubRelayError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@hub.group(name="secret")
def hub_secret() -> None:
    """Hold secret values for the devices that share a workspace.

    Values arrive and leave on this command's own standard input and output.
    They are never written to the workspace repository, to a relay file, or to
    a temporary file, and never appear in output, logs, or error messages.
    """


@hub_secret.command(name="receive")
@click.argument("workspace_id")
def receive_secrets(workspace_id: str) -> None:
    """Store the values a device sends, one framed entry each."""
    results: list[dict[str, Any]] = []
    for entry in _read_stdin_entries():
        results.append(_store_entry(workspace_id, entry))
    _write_json_line({"results": results})


@hub_secret.command(name="send")
@click.argument("workspace_id")
@click.option("--key", "keys", multiple=True, help="A logical key to send back.")
def send_secrets(workspace_id: str, keys: tuple[str, ...]) -> None:
    """Write the requested values back to the device, one framed entry each."""
    stream = click.get_binary_stream("stdout")
    for key in keys:
        try:
            value, generation = secret_host.read_secret(workspace_id, key)
        except secret_host.HubSecretMissingError:
            secret_stream.write_entry(stream, key, {"error": "missing"})
            continue
        except InvalidSecretKeyError:
            secret_stream.write_entry(stream, key, {"error": "invalid"})
            continue
        except SecretStoreError as exc:
            secret_stream.write_entry(stream, key, {"error": _store_error(exc)})
            continue
        except (ValueError, host.HubError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        secret_stream.write_entry(
            stream,
            key,
            {"generation": generation},
            value.encode("utf-8"),
        )
    stream.flush()


@hub_secret.command(name="list")
@click.argument("workspace_id")
def list_secrets(workspace_id: str) -> None:
    """Report which generation this hub holds for each key, without values."""
    try:
        held = secret_host.generations(workspace_id)
    except (ValueError, host.HubError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    status = keyring_status()
    _write_json_line(
        {
            "workspace_id": workspace_id,
            "keys": held,
            "secret_store": {
                "available": bool(status["available"]),
                "locked": bool(status["locked"]),
            },
        }
    )


def _read_stdin_entries() -> list[secret_stream.SecretEntry]:
    """Read the whole framed request, refusing anything shaped differently."""
    try:
        return list(secret_stream.read_entries(click.get_binary_stream("stdin").read()))
    except secret_stream.SecretStreamError as exc:
        raise click.ClickException(str(exc)) from exc


def _store_entry(workspace_id: str, entry: secret_stream.SecretEntry) -> dict[str, Any]:
    """Store one sent value, reporting the outcome without the value."""
    try:
        generation = secret_host.store_secret(
            workspace_id,
            entry.key,
            base_generation=_generation(entry, "base"),
            candidate_generation=_generation(entry, "candidate"),
            value=entry.value.decode("utf-8"),
        )
    except secret_host.HubSecretConflictError:
        return {"key": entry.key, "status": "conflict"}
    except InvalidSecretKeyError:
        # One unusable name does not cost the sender the rest of the batch.
        return {"key": entry.key, "status": "invalid"}
    except SecretStoreError as exc:
        return {"key": entry.key, "status": _store_error(exc)}
    except UnicodeDecodeError:
        return {"key": entry.key, "status": "invalid"}
    except (ValueError, host.HubError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    return {"key": entry.key, "status": "stored", "generation": generation}


def _generation(entry: secret_stream.SecretEntry, name: str) -> int:
    value = entry.header.get(f"{name}_generation")
    if isinstance(value, bool) or not isinstance(value, int):
        raise click.ClickException(
            f"the entry for {entry.key} declares no {name} generation"
        )
    return value


def _store_error(exc: SecretStoreError) -> str:
    """Name why the hub's own keychain refused, never quoting the value."""
    return "locked" if is_locked_error(exc) else "store_unavailable"


def _write_json_line(payload: dict[str, Any]) -> None:
    stream = click.get_binary_stream("stdout")
    stream.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    )
    stream.flush()


def _hub_payload(settings: host.HubSettings) -> dict[str, Any]:
    return {
        "hosted": True,
        "hub_root": str(host.hub_root()),
        "hub_id": settings.hub_id,
        "created_at": settings.created_at,
        "ssh_endpoint": settings.ssh_endpoint,
        "workspaces": host.list_workspace_ids(),
    }


def _owner_payload(owner: relay.ServiceOwner | None) -> dict[str, Any] | None:
    return None if owner is None else owner.model_dump()


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
    "owner": "Owner",
    "claimed": "Claimed",
}
