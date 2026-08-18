"""The one OpenSSH client, and how Git is told to use it."""

from __future__ import annotations

import pytest

from guildbotics.utils import openssh


def test_the_client_path_reaches_git_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Git splits this value the way a shell would, and a Windows path is full
    of backslashes. Unquoted, they are read as escapes: the client arrives as
    ``C:WindowsSystem32OpenSSHssh.exe``, and Git reports every fetch and push
    as a repository it could not read from -- with the hub blamed for it."""
    monkeypatch.setattr(
        openssh, "ssh_executable", lambda: r"C:\Windows\System32\OpenSSH\ssh.exe"
    )

    assert (
        openssh.git_ssh_command()
        == r'"C:\Windows\System32\OpenSSH\ssh.exe" -o BatchMode=yes'
    )


def test_git_never_stops_to_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synchronization runs on a background thread: a client that paused for an
    unknown host key would hang it instead of reporting an unreachable hub."""
    monkeypatch.setattr(openssh, "ssh_executable", lambda: "/usr/bin/ssh")

    assert openssh.git_ssh_command() == '"/usr/bin/ssh" -o BatchMode=yes'
