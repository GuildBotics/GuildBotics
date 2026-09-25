"""Shared Click options and their handling for the GuildBotics CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from guildbotics.utils.env_loader import load_guildbotics_env
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.shared_write_lock import SharedWriteBusyError
from guildbotics.utils.workspace_state import (
    WorkspaceState,
    WorkspaceUnresolvedError,
    apply_workspace_for_cli,
)

FormatChoice = click.Choice(["json", "markdown"])
#: Every GuildBotics command group's context settings: the member group can be
#: run on its own, without the root CLI it would otherwise inherit them from.
CLI_CONTEXT_SETTINGS = {"show_default": True}


class SharedWriteBusyGroup(click.Group):
    """A command group, with the one failure every command shares.

    Any command that changes a workspace's shared files can find another
    writer -- the synchronization queue, or a second GuildBotics process --
    holding the lock for longer than the wait. That is not a fault of the
    command, and every command would otherwise need its own handler for a
    condition none of them causes, so it is answered once here. The API layer
    answers the same condition once, in its own exception handler.
    """

    def invoke(self, ctx: click.Context) -> object:
        try:
            return super().invoke(ctx)
        except SharedWriteBusyError as exc:
            raise click.ClickException(t("cli.shared.write_busy")) from exc


workspace_option = click.option(
    "--workspace",
    "workspace_dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Workspace root to use instead of the persisted active workspace.",
)


def format_option(default: str) -> Callable[[Any], Any]:
    """``--format`` option with the shared output-format help text."""
    return click.option(
        "--format",
        "output_format",
        type=FormatChoice,
        default=default,
        help="Output format.",
    )


def apply_workspace_option(workspace_dir: Path | None) -> WorkspaceState | None:
    """Select the workspace a command group runs against and load its env.

    Args:
        workspace_dir: Value of :data:`workspace_option`, or ``None`` to use the
            persisted active workspace.

    Returns:
        The applied workspace, or ``None`` when the current directory is used.

    Raises:
        click.ClickException: If the requested workspace does not exist.
    """
    try:
        applied = apply_workspace_for_cli(workspace_dir)
    except NotADirectoryError as exc:
        raise click.ClickException(f"workspace does not exist: {exc}") from exc
    except WorkspaceUnresolvedError as exc:
        raise click.ClickException(str(exc)) from exc
    load_guildbotics_env(override=False)
    return applied
