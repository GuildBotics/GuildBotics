"""The ``guildbotics secrets`` commands: this machine's own secret store.

Secret values never appear in the arguments, the output, or an error of these
commands. A value entered here is read from a prompt or a file.
"""

from __future__ import annotations

from pathlib import Path

import click

from guildbotics.cli._options import apply_workspace_option
from guildbotics.utils.fileio import (
    get_identity_root,
    get_workspace_config_dir,
    get_workspace_root,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.keychain import SecretStoreError, SecretValueTooLargeError
from guildbotics.utils.person_id import is_member_secret
from guildbotics.utils.secret_store import (
    SECRETS_INDEX_FILENAME,
    KeyringSecretStore,
    format_env_line,
    keyring_status,
    read_env_values,
    resolve_secret_store,
    write_env_text,
)


class _SecretsContext:
    def __init__(self, root: Path, *, identities: bool):
        self.root = root
        #: The identities' store, which every workspace shares, rather than
        #: the workspace's.
        self.identities = identities
        self.config_dir = get_workspace_config_dir(root)

    def store(self) -> KeyringSecretStore:
        return resolve_secret_store(self.config_dir)

    def set_values(self, values: dict[str, str]) -> None:
        """Store values, keeping the members' keys out of a workspace's store.

        A member's key in both stores leaves which value a process runs with
        to load order, so it is refused here rather than resolved there.
        """
        if not self.identities and (
            misplaced := sorted(key for key in values if is_member_secret(key))
        ):
            raise click.ClickException(
                t("cli.secrets.member_key", keys=", ".join(misplaced))
            )
        _set_values(self.store(), values)


class _SecretStoreGroup(click.Group):
    """A command group that reports a store it cannot use as a command error.

    Every command reaches the store, and a locked keychain or an index that
    cannot be read stops each of them the same way.
    """

    def invoke(self, ctx: click.Context) -> object:
        try:
            return super().invoke(ctx)
        except SecretStoreError as exc:
            raise click.ClickException(
                t("cli.secrets.store_failed", error=exc)
            ) from exc


@click.group(cls=_SecretStoreGroup)
@click.pass_context
@click.option(
    "--workspace",
    "workspace_dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Workspace root to use instead of the persisted active workspace.",
)
@click.option(
    "--identities",
    is_flag=True,
    help=(
        "Manage the secrets every workspace shares -- the members' credentials "
        "and the services' connections -- instead of the workspace's."
    ),
)
def secrets(ctx: click.Context, workspace_dir: Path | None, identities: bool) -> None:
    """Manage workspace secrets (API keys and tokens)."""
    if identities:
        ctx.obj = _SecretsContext(get_identity_root(), identities=True)
        return
    applied_workspace = apply_workspace_option(workspace_dir, load_env=False)
    root = applied_workspace.workspace if applied_workspace else get_workspace_root()
    ctx.obj = _SecretsContext(root, identities=False)


@secrets.command(name="status")
@click.pass_obj
def secrets_status(env: _SecretsContext) -> None:
    """Show OS secret-store status for this workspace."""
    status = keyring_status()
    click.echo(f"workspace: {env.root}")
    click.echo(
        f"os_secret_store: {'available' if status['available'] else 'unavailable'}"
    )
    click.echo(f"locked: {'yes' if status['locked'] else 'no'}")
    click.echo(f"location: {env.config_dir / SECRETS_INDEX_FILENAME}")
    try:
        click.echo(f"stored keys: {len(env.store().keys())}")
    except SecretStoreError as exc:
        click.echo(f"stored keys: unknown ({exc})")


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
    """Store a secret value (prompts when VALUE is omitted).

    KEY comes first: naming the value first would store it under a name that
    is itself the secret.
    """
    if from_file is not None and value is not None:
        raise click.ClickException("Specify VALUE or --from-file, not both.")
    if from_file is not None:
        value = from_file.read_text(encoding="utf-8")
    elif value is None:
        value = click.prompt(f"Value for {key}", hide_input=True)
    env.set_values({key: value})
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
    click.echo("Delete the file once you no longer need it.")


@secrets.command(name="import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_obj
def secrets_import(env: _SecretsContext, file: Path) -> None:
    """Import secrets from a dotenv-format exchange file into the OS store."""
    values = read_env_values(file)
    if not values:
        raise click.ClickException(f"no values found in {file}")
    env.set_values(values)
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
