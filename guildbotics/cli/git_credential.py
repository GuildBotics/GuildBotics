"""The GuildBotics CLI as the human member's git credential helper."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable, Coroutine
from typing import IO, Any

import click

from guildbotics.integrations.factory import human_git_credential
from guildbotics.runtime.code_hosting_service import GitCredential
from guildbotics.runtime.port import ServiceError
from guildbotics.utils.i18n_tool import t


def answer(
    action: str,
    request: IO[str],
    reply: IO[bytes],
    credential_for: Callable[[str], Coroutine[Any, Any, GitCredential | None]],
) -> None:
    """Answer git's credential helper ``action`` (gitcredentials(7)): git's
    attributes are read from ``request`` and, for ``get``, what
    ``credential_for`` hands for the remote's origin is written to ``reply``.
    Nothing is kept, so ``store``, ``erase``, and any other action do nothing.

    A failure is said on stderr and ends git's search with ``quit``: git would
    otherwise ask the terminal for a password nobody has.
    """
    attributes: dict[str, str] = {}
    for line in request:
        if not (line := line.rstrip("\r\n")):
            break
        key, _, value = line.partition("=")
        attributes[key] = value
    if action != "get":
        return
    origin = f"{attributes.get('protocol', '')}://{attributes.get('host', '')}"
    try:
        credential = asyncio.run(credential_for(origin))
    except ServiceError as exc:
        click.echo(str(exc), err=True)
        reply.write(b"quit=1\n")
        return
    if credential is None:
        # Git goes on to its own prompt; say why it is asked.
        click.echo(t("cli.git_credential.not_served", origin=origin), err=True)
        return
    # LF, not the platform's newline: git reads the lines as it wrote them.
    reply.write(
        f"username={credential.username}\npassword={credential.password}\n".encode()
    )


@click.command(name="git-credential")
@click.argument("action")
def git_credential(action: str) -> None:
    """Hand git the human member's credential for a remote of a service
    GuildBotics is connected to on this machine (ACTION is git's: get,
    store, or erase).

    Git runs this as a credential helper: add it, scoped to the service's
    URL, at the end of the global git configuration. Setup shows the lines to
    add.
    """
    answer(
        action,
        # Git writes UTF-8 whatever the locale's encoding is.
        io.TextIOWrapper(
            click.get_binary_stream("stdin"), encoding="utf-8", errors="replace"
        ),
        click.get_binary_stream("stdout"),
        human_git_credential,
    )
