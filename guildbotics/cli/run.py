"""The ``run`` command: one custom command, on the Desktop when open, else locally."""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

import click

from guildbotics.cli._options import selected_workspace
from guildbotics.cli.desktop_commands import run_on_desktop
from guildbotics.drivers import (
    CommandError,
    PersonExecutionNotAllowedError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
)
from guildbotics.editions import get_edition
from guildbotics.runtime.local_command_executor import LocalCommandExecutor


@click.command()
@click.option(
    "--person",
    "person_option",
    help=(
        "Person ID or name to run the custom command as. Defaults to the team's "
        "default_person_id, else its first active non-human member in ID order."
    ),
)
@click.option(
    "--cwd",
    type=str,
    default=None,
    help="Specify the working directory for the custom command.",
)
@click.argument("custom_command", required=True)
@click.argument("command_args", nargs=-1)
def run(
    person_option: str | None,
    cwd: str | None,
    custom_command: str,
    command_args: tuple[str, ...],
) -> None:
    """Run a command through the matching Desktop when open, otherwise locally."""
    workspace = selected_workspace()
    command_cwd = Path(cwd).expanduser().resolve(strict=False) if cwd else None
    message = "" if sys.stdin.isatty() else sys.stdin.read()
    command_name, inline_person = _parse_command_spec(custom_command)
    output = run_on_desktop(
        workspace,
        command_name,
        command_args,
        person_option or inline_person,
        message,
        command_cwd or Path.cwd(),
    )
    if output is not None:
        if output:
            click.echo(output)
        return
    asyncio.run(
        _run_custom_command(
            custom_command,
            command_args,
            person_option,
            message,
            command_cwd,
        )
    )


async def _run_custom_command(
    command_spec: str,
    command_args: tuple[str, ...],
    person_option: str | None,
    message: str,
    cwd: Path | None = None,
) -> None:
    command_name, inline_person = _parse_command_spec(command_spec)
    edition = get_edition()
    context = edition.get_context(message)
    identifier = person_option or inline_person

    try:
        outcome = await LocalCommandExecutor().run(
            context,
            command_name=command_name,
            command_args=command_args,
            person_identifier=identifier,
            cwd=cwd,
        )
    except PersonSelectionRequiredError as exc:
        available = ", ".join(exc.available) if exc.available else "none"
        raise click.ClickException(
            "Specify a person using '--person' or '<command>@person'."
            f" Available: {available}"
        ) from exc
    except PersonNotFoundError as exc:
        available = ", ".join(exc.available) if exc.available else "none"
        raise click.ClickException(
            f"Person '{exc.identifier}' not found. Available: {available}"
        ) from exc
    except PersonExecutionNotAllowedError as exc:
        raise click.ClickException(str(exc)) from exc
    except CommandError as exc:
        traceback.print_exc()
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive guard
        traceback.print_exc()
        raise click.ClickException(str(exc)) from exc

    if outcome.text_output:
        click.echo(outcome.text_output)


def _parse_command_spec(command_spec: str) -> tuple[str, str | None]:
    parts = command_spec.split("@", 1)
    name = parts[0].strip()
    if not name:
        raise click.ClickException("Command name cannot be empty.")
    person = parts[1].strip() if len(parts) > 1 else None
    if person == "":
        person = None
    return name, person
