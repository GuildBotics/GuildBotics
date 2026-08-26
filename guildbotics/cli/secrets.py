"""The ``guildbotics secrets`` commands.

Reading and writing this machine's own secret store, and the two explicit
transfers with the hub. The transfers are here as well as in the Desktop
because a machine can have no Desktop at all: a headless Linux device joins a
workspace, finds it holds none of the values, and fetches them from here.

Secret values never appear in the arguments, the output, or an error of these
commands. A value entered here is read from a prompt or a file, and a value
arriving from the hub goes straight into the OS secret store.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import click

from guildbotics.hub import HubUnreachableError, connection
from guildbotics.hub.secret_host import HubSecretError
from guildbotics.secrets import SecretTransfer, SecretTransferOutcome, hub_secret_client
from guildbotics.sync import hub_remote_url
from guildbotics.sync.activation import (
    ONE_SHOT_LOCK_TIMEOUT_SECONDS,
    commit_and_push_once,
    synchronize_once,
)
from guildbotics.utils.fileio import get_workspace_config_dir, get_workspace_root
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.keychain import SecretStoreError, SecretValueTooLargeError
from guildbotics.utils.openssh import OpenSshNotFoundError
from guildbotics.utils.secret_store import (
    KeyringSecretStore,
    SecretKeyState,
    SecretKeyStatus,
    format_env_line,
    keyring_status,
    read_env_values,
    resolve_secret_store,
    write_env_text,
)
from guildbotics.utils.sync_lock import SyncRepositoryBusyError
from guildbotics.utils.workspace_state import (
    WorkspaceUnresolvedError,
    apply_workspace_for_cli,
)


class _SecretsContext:
    def __init__(self, root: Path):
        self.root = root
        self.config_dir = get_workspace_config_dir()

    def store(self) -> KeyringSecretStore:
        return resolve_secret_store(self.config_dir)

    def transfer(self) -> SecretTransfer:
        """Return the transfer for this workspace's hub.

        Raises:
            click.ClickException: When the workspace has no hub. There is
                nowhere to send to and nothing to fetch from, and saying so is
                more useful than an empty result.
        """
        remote_url = hub_remote_url(self.root)
        if not remote_url:
            raise click.ClickException(
                "This workspace is not connected to a hub. "
                "Connect it before transferring secrets."
            )
        workspace_id = connection.workspace_id_from_remote_url(remote_url)
        location = connection.location_from_remote_url(remote_url, workspace_id)
        return SecretTransfer(self.store(), hub_secret_client(location, workspace_id))


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
    states: list[SecretKeyState] = []
    try:
        store = env.store()
        key_count = len(store.keys())
        stale = store.stale_keys()
        states = store.key_states()
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
    for state in states:
        if state.status is SecretKeyStatus.READY:
            continue
        click.echo(
            f"  {state.key}: {state.status} "
            f"(shared {state.shared_generation}, here {state.local_generation})"
        )
    if states and any(state.status is not SecretKeyStatus.READY for state in states):
        click.echo(
            "Run `guildbotics secrets pull` to take what the hub holds, or "
            "`guildbotics secrets push` to give it what was entered here."
        )


@secrets.command(name="push")
@click.argument("keys", nargs=-1)
@click.pass_obj
def secrets_push(env: _SecretsContext, keys: tuple[str, ...]) -> None:
    """Send values entered on this machine to the hub (all it lacks by default).

    The shared files are refreshed first. Which generation a value is sent from
    is read out of ``config/secrets.yml``, and a machine that has been offline
    would otherwise send from a generation the others have moved past.
    """
    transfer = env.transfer()
    _refresh()
    outcomes = _run_transfer(
        lambda: transfer.send(list(keys)) if keys else transfer.send_pending()
    )
    if not outcomes:
        click.echo("Nothing to send: the hub holds every value entered here.")
        return
    _report(outcomes)
    # The generations just published live in the shared config file, so a
    # machine with no running service still gets them to the hub.
    _synchronize_generations()


@secrets.command(name="pull")
@click.argument("keys", nargs=-1)
@click.pass_obj
def secrets_pull(env: _SecretsContext, keys: tuple[str, ...]) -> None:
    """Fetch values from the hub into this machine's OS secret store.

    With no KEYS this is the whole of setting up a new device: every key this
    machine has no value for, or holds an older value for, arrives in one
    exchange and is stored without anything being retyped.

    The shared files are taken first: only the generation they name is ever
    adopted, so an old copy of them would leave nothing to fetch.
    """
    transfer = env.transfer()
    _refresh()
    outcomes = _run_transfer(
        lambda: transfer.fetch(list(keys)) if keys else transfer.fetch_missing()
    )
    if not outcomes:
        click.echo("Nothing to fetch: this machine holds every shared value.")
        return
    _report(outcomes)


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


def _run_transfer(
    action: Callable[[], list[SecretTransferOutcome]],
) -> list[SecretTransferOutcome]:
    """Run one transfer, reporting a hub failure as a command error."""
    try:
        return action()
    except (HubSecretError, HubUnreachableError, OpenSshNotFoundError) as exc:
        raise click.ClickException(str(exc)) from exc


def _report(outcomes: list[SecretTransferOutcome]) -> None:
    """Print one line per key: its name and what happened, never its value."""
    for outcome in outcomes:
        generation = (
            "" if outcome.generation is None else f" (generation {outcome.generation})"
        )
        click.echo(f"{outcome.key}: {outcome.status}{generation}")


def _refresh() -> None:
    """Take the current shared files before deciding what to send.

    Best effort: a hub that cannot be reached, or a repository something else
    is holding, leaves the decision on what this machine already has -- which
    is where it would have been made anyway.
    """
    with suppress(SyncRepositoryBusyError, HubSecretError, HubUnreachableError):
        synchronize_once(timeout=ONE_SHOT_LOCK_TIMEOUT_SECONDS)


def _synchronize_generations() -> None:
    """Push the metadata this transfer wrote, on a machine with no service."""
    try:
        commit_and_push_once(timeout=ONE_SHOT_LOCK_TIMEOUT_SECONDS)
    except SyncRepositoryBusyError:
        click.echo(
            "The generations were recorded and will be sent by the "
            "synchronization already running on this machine."
        )


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
