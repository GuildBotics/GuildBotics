from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from guildbotics.intelligences.functions import to_text

_WINDOWS = os.name == "nt"


def find_shell() -> str | None:
    """Return the shell that runs and checks ``.sh`` commands, or None.

    On Windows this is Git for Windows' bash, looked for before whatever
    ``bash`` the PATH resolves to. GuildBotics already requires Git, and the
    name is ambiguous there: ``C:\\Windows\\System32\\bash.exe`` is the WSL
    launcher, which answers to ``bash`` without being a shell this can use.
    Git's own is not always on PATH either -- ``git.exe`` is exposed through
    ``cmd`` while ``bash.exe`` stays beside it -- so it is found through
    ``git``. Running a script and checking one must not disagree about which
    shell this device has, so both ask here.
    """
    if _WINDOWS:
        git = shutil.which("git")
        if git is not None:
            # Git for Windows answers to ``git`` from ``cmd``, from ``bin`` and
            # from ``mingw64/bin``, and keeps ``bash.exe`` in ``bin`` under the
            # same root, so the root is one or two levels above ``git.exe``.
            directory = Path(git).parent
            for root in (directory.parent, directory.parent.parent):
                found = shutil.which("bash", path=str(root / "bin"))
                if found is not None:
                    return found
    return shutil.which("bash")


def stringify_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, BaseModel):
        return to_text(output)
    if isinstance(output, dict):
        return to_text(output)
    if isinstance(output, list):
        if output and isinstance(output[0], (BaseModel, dict)):
            return to_text(output)
        return "\n".join(str(item) for item in output)
    return str(output)
