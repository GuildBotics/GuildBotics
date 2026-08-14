from __future__ import annotations

from pathlib import Path

import click

from guildbotics.utils.env_loader import workspace_config_dir
from guildbotics.utils.fileio import get_workspace_root
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.keychain import SecretStoreError, SecretValueTooLargeError
from guildbotics.utils.secret_store import (
    KeyringSecretStore,
    format_env_line,
    keyring_status,
    read_env_values,
    resolve_secret_store,
    write_env_text,
)
from guildbotics.utils.workspace_state import (
    WorkspaceUnresolvedError,
    apply_workspace_for_cli,
)


class _SecretsContext:
    def __init__(self, root: Path):
        self.root = root
        self.config_dir = workspace_config_dir()

    def store(self) -> KeyringSecretStore:
        return resolve_secret_store(self.config_dir)


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
    except WorkspaceUnresolvedError as exc:
        raise click.ClickException(str(exc)) from exc
    root = applied_workspace.workspace if applied_workspace else get_workspace_root()
    ctx.obj = _SecretsContext(root.resolve())


@secrets.command(name="status")
@click.pass_obj
def secrets_status(env: _SecretsContext) -> None:
    """Show OS secret-store status for this workspace."""
    status = keyring_status()
    stale: list[str] = []
    try:
        store = env.store()
        key_count = len(store.keys())
        stale = store.stale_keys()
        location = str(store.location)
    except SecretStoreError:
        key_count = 0
        location = str(env.config_dir / "secrets.yml")
    click.echo(f"workspace: {env.root}")
    click.echo(
        f"os_secret_store: {'available' if status['available'] else 'unavailable'}"
    )
    click.echo(f"locked: {'yes' if status['locked'] else 'no'}")
    click.echo(f"location: {location}")
    click.echo(f"stored keys: {key_count}")
    click.echo(f"stale keys: {len(stale)}")
    for key in stale:
        click.echo(
            f"  {key}: this device holds an older generation; "
            "run `guildbotics secrets set` or `secrets import` here"
        )


@secrets.command(name="list")
@click.pass_obj
def secrets_list(env: _SecretsContext) -> None:
    """List the names of the stored secrets."""
    for key in sorted(env.store().keys()):
        click.echo(key)


@secrets.command(name="set")
@click.argument("key")
@click.argument("value", required=False)
@click.option(
    "--from-file",
    "from_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read the secret value from a file instead of VALUE.",
)
@click.pass_obj
def secrets_set(
    env: _SecretsContext,
    key: str,
    value: str | None,
    from_file: Path | None,
) -> None:
    """Store a secret value (prompts when VALUE is omitted)."""
    if from_file is not None and value is not None:
        raise click.ClickException("Specify VALUE or --from-file, not both.")
    if from_file is not None:
        value = from_file.read_text(encoding="utf-8")
    elif value is None:
        value = click.prompt(f"Value for {key}", hide_input=True)
    _set_values(env.store(), {key: value})
    click.echo(f"Stored {key}.")


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
    """Export stored secrets in dotenv format (exchange format only)."""
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
    """Import secrets from a dotenv-format exchange file into the OS store."""
    values = read_env_values(file)
    if not values:
        raise click.ClickException(f"no values found in {file}")
    _set_values(env.store(), values)
    click.echo(f"Imported {len(values)} secrets.")
    click.echo(f"Delete {file} once you no longer need it.")


def _set_values(store: KeyringSecretStore, values: dict[str, str]) -> None:
    """Store values with safe CLI errors that never include secret contents."""
    try:
        store.set_many(values)
    except SecretValueTooLargeError as exc:
        raise click.ClickException(
            t(
                "cli.secrets.value_too_large",
                secret_key=exc.key,
                size=exc.size,
                limit=exc.limit,
            )
        ) from exc
    except SecretStoreError as exc:
        raise click.ClickException(t("cli.secrets.store_failed", error=exc)) from exc
