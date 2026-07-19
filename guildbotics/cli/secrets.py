from __future__ import annotations

from pathlib import Path

import click

from guildbotics.utils.env_loader import workspace_config_dir
from guildbotics.utils.secret_store import (
    KEYRING_BACKEND,
    SecretStore,
    configured_secrets_backend,
    format_env_line,
    keyring_available,
    read_env_values,
    resolve_secret_store,
    write_env_text,
    write_env_values,
)
from guildbotics.utils.workspace_state import apply_workspace_for_cli


class _SecretsContext:
    def __init__(self, root: Path):
        self.root = root
        self.config_dir = workspace_config_dir(root)
        self.env_file = root / ".env"

    def store(self) -> SecretStore:
        return resolve_secret_store(self.config_dir, self.env_file)


@click.group()
@click.pass_context
@click.option(
    "--workspace",
    "workspace_dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Workspace root to use instead of the persisted active workspace.",
)
def secrets(ctx: click.Context, workspace_dir: Path | None) -> None:
    """Manage workspace secrets (API keys and tokens)."""
    try:
        applied_workspace = apply_workspace_for_cli(workspace_dir)
    except NotADirectoryError as exc:
        raise click.ClickException(f"workspace does not exist: {exc}") from exc
    root = applied_workspace.workspace if applied_workspace else Path.cwd()
    ctx.obj = _SecretsContext(root.resolve())


@secrets.command(name="status")
@click.pass_obj
def secrets_status(env: _SecretsContext) -> None:
    """Show the secret backend used by this workspace."""
    configured = configured_secrets_backend(env.config_dir)
    store = env.store()
    click.echo(f"workspace: {env.root}")
    click.echo(f"backend: {store.backend}" + ("" if configured else " (default)"))
    click.echo(f"location: {store.location}")
    click.echo(f"keychain available: {'yes' if keyring_available() else 'no'}")
    click.echo(f"stored keys: {len(store.keys())}")


@secrets.command(name="list")
@click.pass_obj
def secrets_list(env: _SecretsContext) -> None:
    """List the names of the stored secrets."""
    for key in sorted(env.store().keys()):
        click.echo(key)


@secrets.command(name="set")
@click.argument("key")
@click.argument("value", required=False)
@click.pass_obj
def secrets_set(env: _SecretsContext, key: str, value: str | None) -> None:
    """Store a secret value (prompts when VALUE is omitted)."""
    if value is None:
        value = click.prompt(f"Value for {key}", hide_input=True)
    store = env.store()
    store.set(key, value)
    if store.backend == KEYRING_BACKEND:
        _strip_env_file_keys(env.env_file, [key])
    click.echo(f"Stored {key} ({store.backend}).")


@secrets.command(name="delete")
@click.argument("key")
@click.pass_obj
def secrets_delete(env: _SecretsContext, key: str) -> None:
    """Delete a stored secret."""
    env.store().delete(key)
    click.echo(f"Deleted {key}.")


@secrets.command(name="export")
@click.option(
    "--file",
    "file_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write to a file (created with owner-only permissions) instead of stdout.",
)
@click.pass_obj
def secrets_export(env: _SecretsContext, file_path: Path | None) -> None:
    """Export stored secrets in dotenv format (for moving machines)."""
    values = env.store().values()
    content = "\n".join(format_env_line(key, values[key]) for key in sorted(values))
    if file_path is None:
        click.echo(content)
        return
    write_env_text(file_path, content + "\n")
    click.echo(f"Exported {len(values)} secrets to {file_path}.")
    click.echo("Delete the file after importing it on the target machine.")


@secrets.command(name="import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_obj
def secrets_import(env: _SecretsContext, file: Path) -> None:
    """Import secrets from a dotenv-format file into the workspace store."""
    values = read_env_values(file)
    if not values:
        raise click.ClickException(f"no values found in {file}")
    store = env.store()
    for key, value in values.items():
        store.set(key, value)
    if store.backend == KEYRING_BACKEND:
        _strip_env_file_keys(env.env_file, list(values))
    click.echo(f"Imported {len(values)} secrets ({store.backend}).")
    if store.backend == KEYRING_BACKEND:
        click.echo(f"Delete {file} once you no longer need it.")


def _strip_env_file_keys(env_file: Path, keys: list[str]) -> None:
    """Drop keys from .env so no plaintext copy shadows the keychain."""
    if not env_file.exists():
        return
    env_values = read_env_values(env_file)
    remaining = {key: value for key, value in env_values.items() if key not in keys}
    if len(remaining) != len(env_values):
        write_env_values(env_file, remaining)
