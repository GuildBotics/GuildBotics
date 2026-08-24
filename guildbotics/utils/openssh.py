"""The one OpenSSH client GuildBotics talks to a hub with.

Git and the hub commands must not end up on different SSH clients. On Windows
that is easy to get wrong: Git for Windows ships its own ``ssh.exe`` with its
own ``known_hosts``, so a host trusted for the hub commands would be unknown to
``git fetch``. Resolving the client here, and handing the same one to Git
through ``GIT_SSH_COMMAND``, keeps one client and one set of trusted hosts.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

#: Where Windows keeps the OpenSSH client that owns the user's ``known_hosts``.
_WINDOWS_OPENSSH = Path("C:/Windows/System32/OpenSSH/ssh.exe")

#: How long any one-shot command that reaches a hub may take. The subprocess
#: is the boundary because ssh options cannot be: ``ServerAlive*`` only
#: watches established connections, and ``ConnectTimeout`` starts after name
#: resolution -- yet a hub that vanished from the network can leave mDNS
#: resolution itself pending indefinitely.
REMOTE_COMMAND_TIMEOUT_SECONDS = 30.0


class OpenSshNotFoundError(RuntimeError):
    """Raised when this machine has no OpenSSH client to reach a hub with."""


def ssh_executable() -> str:
    """Return the OpenSSH client this machine uses for every hub connection.

    Raises:
        OpenSshNotFoundError: When no client is installed.
    """
    if sys.platform.startswith("win") and _WINDOWS_OPENSSH.is_file():
        return str(_WINDOWS_OPENSSH)
    found = shutil.which("ssh")
    if found is None:
        raise OpenSshNotFoundError(
            "No OpenSSH client was found. Install OpenSSH to connect to a hub."
        )
    return found


def keygen_executable() -> str | None:
    """Return ``ssh-keygen``, or None when it is not installed."""
    return _companion("ssh-keygen")


def git_ssh_command() -> str:
    """Return the value Git needs so it uses the same client as everything else.

    Batch mode is part of it: synchronization runs on a background thread, and
    a client that stopped to ask about an unknown host key or a passphrase
    would hang that thread instead of reporting the hub as unreachable.
    """
    # Git splits this value the way a shell would, so the path is always
    # quoted rather than only when it holds a space. A Windows path is full of
    # backslashes, and unquoted they are read as escapes: the client arrives as
    # ``C:WindowsSystem32OpenSSHssh.exe``, and every fetch and push fails as a
    # repository that could not be read from.
    return f'"{ssh_executable()}" -o BatchMode=yes'


def _companion(name: str) -> str | None:
    """Find a tool next to the resolved client before falling back to PATH."""
    sibling = Path(ssh_executable()).with_name(name)
    if sibling.is_file():
        return str(sibling)
    if sys.platform.startswith("win"):
        sibling = sibling.with_suffix(".exe")
        if sibling.is_file():
            return str(sibling)
    return shutil.which(name)
