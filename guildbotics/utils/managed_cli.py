"""The GuildBotics CLI that ``scripts/install_cli_assets.py`` installs, as
instructions spell it."""

from __future__ import annotations

import shlex
from pathlib import Path


def managed_cli_command(*args: str) -> str:
    """The managed CLI invoked with ``args``, as a terminal is told to run it.

    Its absolute path is quoted so spaces and shell metacharacters survive.
    """
    return shlex.join([str(Path.home() / ".guildbotics/bin/guildbotics"), *args])
