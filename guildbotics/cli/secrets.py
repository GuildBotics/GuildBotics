from __future__ import annotations

import os
from pathlib import Path

import click

from guildbotics.entities.team import Person
from guildbotics.utils.env_loader import workspace_config_dir
from guildbotics.utils.secret_store import (
    ENV_FILE_BACKEND,
    KEYRING_BACKEND,
    SECRETS_BACKEND_ENV,
    KeyringSecretStore,
    SecretStore,
    configured_secrets_backend,
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
    click.echo(
        f"backend: {store.backend}" + ("" if configured else " (legacy default)")
    )
    click.echo(f"location: {store.location}")
    click.echo(f"keychain available: {'yes' if keyring_available() else 'no'}")
    click.echo(f"stored keys: {len(store.keys())}")
    if store.backend == ENV_FILE_BACKEND and keyring_available():
        click.echo(
            "hint: run 'guildbotics secrets migrate' to move secrets "
            "from .env into the OS keychain."
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


@secrets.command(name="migrate")
@click.option(
    "--key",
    "extra_keys",
    multiple=True,
    help="Additional .env key to migrate (repeatable).",
)
@click.pass_obj
def secrets_migrate(env: _SecretsContext, extra_keys: tuple[str, ...]) -> None:
    """Move secrets from the .env file into the OS keychain."""
    if os.getenv(SECRETS_BACKEND_ENV, "").strip() == ENV_FILE_BACKEND:
        raise click.ClickException(
            f"{SECRETS_BACKEND_ENV}={ENV_FILE_BACKEND} forces the .env backend; "
            "unset it before migrating."
        )
    if not keyring_available():
        raise click.ClickException(
            "No functional OS keychain is available on this machine."
        )
    store = KeyringSecretStore(env.config_dir)
    env_values = read_env_values(env.env_file)
    candidates = [
        key
        for key in [*_managed_secret_keys(env.config_dir), *extra_keys]
        if key in env_values
    ]
    store.ensure_initialized()
    for key in candidates:
        store.set(key, env_values[key])
    imported_pems = _import_private_key_files(store, env_values)
    _strip_env_file_keys(
        env.env_file,
        candidates + [f"{key}_PATH" for key, _ in imported_pems],
    )
    click.echo(f"backend: {KEYRING_BACKEND}")
    for key in candidates:
        click.echo(f"moved: {key}")
    for key, pem_path in imported_pems:
        click.echo(f"moved: {key} (content of {pem_path})")
    if imported_pems:
        click.echo(
            "The PEM files above were copied into the OS keychain; "
            "delete them manually once you no longer need them."
        )
    if not candidates and not imported_pems:
        click.echo("No matching secrets found in .env; backend switched only.")


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
    content = "\n".join(f"{key}={values[key]}" for key in sorted(values))
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


def _import_private_key_files(
    store: SecretStore, env_values: dict[str, str]
) -> list[tuple[str, Path]]:
    """Copy GitHub App PEM files referenced from .env into the keychain.

    The keychain content replaces the file: the caller drops the imported
    ``*_GITHUB_PRIVATE_KEY_PATH`` entries from .env, and the PEM file itself
    is left for the user to delete.
    """
    imported: list[tuple[str, Path]] = []
    for key, value in env_values.items():
        if not key.endswith("_GITHUB_PRIVATE_KEY_PATH") or not value:
            continue
        content_key = key.removesuffix("_PATH")
        pem_path = Path(value).expanduser()
        try:
            pem = pem_path.read_text()
        except OSError as exc:
            click.echo(
                f"warning: skipped {content_key}: cannot read {pem_path} ({exc})"
            )
            continue
        store.set(content_key, pem)
        imported.append((content_key, pem_path))
    return imported


def _strip_env_file_keys(env_file: Path, keys: list[str]) -> None:
    """Drop keys from .env so no plaintext copy shadows the keychain."""
    if not env_file.exists():
        return
    env_values = read_env_values(env_file)
    remaining = {key: value for key, value in env_values.items() if key not in keys}
    if len(remaining) != len(env_values):
        write_env_values(env_file, remaining)


def _managed_secret_keys(config_dir: Path) -> list[str]:
    """Secret env keys this workspace is known to use.

    Provider API keys come from the provider catalog; person tokens follow the
    ``<PERSON_ID>_<SUFFIX>`` convention for each configured member.
    """
    from guildbotics.intelligences.llm_providers import provider_env_keys

    keys = list(provider_env_keys(config_dir).values())
    members_dir = config_dir / "team/members"
    if members_dir.is_dir():
        for member_dir in sorted(members_dir.iterdir()):
            if not (member_dir / "person.yml").is_file():
                continue
            prefix = member_dir.name.replace("-", "_").upper()
            keys.extend(f"{prefix}_{suffix}" for suffix in Person.SECRET_ENV_SUFFIXES)
    return keys
