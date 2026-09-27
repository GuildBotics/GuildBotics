"""GuildBotics' own processes in a command's microVM."""

from __future__ import annotations

import asyncio

from guildbotics.intelligences.agent_environment.snapshot import CODE_ROOT, VENV
from guildbotics.intelligences.agent_runtime.command_guest import EnvironmentGuest


def test_guildbotics_runs_with_its_own_python_and_code() -> None:
    """The snapshot's Python, pointed at the code every microVM mounts, and
    writing nothing beside that read-only code."""
    loop = asyncio.new_event_loop()
    try:
        guest = EnvironmentGuest(loop, lambda: None)

        argv = guest.python("guildbotics.capabilities.artifact_archive", "/work/a b")
    finally:
        loop.close()

    assert argv == [
        "env",
        f"PYTHONPATH={CODE_ROOT}",
        "PYTHONDONTWRITEBYTECODE=1",
        f"{VENV}/bin/python",
        "-m",
        "guildbotics.capabilities.artifact_archive",
        "/work/a b",
    ]
