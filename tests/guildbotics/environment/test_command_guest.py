"""GuildBotics' own processes in a command's microVM."""

from __future__ import annotations

import asyncio

from guildbotics.environment.command_guest import EnvironmentGuest
from guildbotics.environment.snapshot import VENV


def test_guildbotics_runs_with_its_own_python_and_code() -> None:
    """The snapshot's Python, which finds the code every microVM mounts by
    itself, writing nothing beside that read-only code; no variable says
    either, so nothing the process starts inherits them."""
    loop = asyncio.new_event_loop()
    try:
        guest = EnvironmentGuest(loop, lambda: None)

        argv = guest.python("guildbotics.capabilities.artifact_archive", "/work/a b")
    finally:
        loop.close()

    assert argv == [
        f"{VENV}/bin/python",
        "-B",
        "-m",
        "guildbotics.capabilities.artifact_archive",
        "/work/a b",
    ]
