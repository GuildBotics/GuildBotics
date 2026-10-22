"""The GuildBotics CLI that Desktop installs, as instructions spell it."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path


def managed_cli_command(*args: str, platform: str | None = None) -> str:
    """The managed CLI invoked with ``args``, as a terminal of ``platform``
    (this machine's by default) is told to run it.

    Windows installers put the CLI on PATH. Unix Desktop installs it under
    home; its absolute path is quoted so spaces and shell metacharacters
    survive.
    """
    if (platform or sys.platform) == "win32":
        return " ".join(["guildbotics", *args])
    return shlex.join([str(Path.home() / ".guildbotics/bin/guildbotics"), *args])
