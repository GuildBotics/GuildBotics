"""Reaching a hub from a device.

A hub is either this same machine or another one over OpenSSH, and the two
differ in one place only: how a command runs and what a Git remote URL looks
like. Everything above this module names a :class:`HubLocation` and stops
caring which of the two it has.

OpenSSH is the whole of the transport. There is no GuildBotics protocol here,
no port to open, and no credential of our own: reaching a hub means the user
can already log into that machine, and revoking a lost device means removing
its public key there.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from guildbotics.hub import host
from guildbotics.utils.openssh import (
    keygen_executable,
    keyscan_executable,
    ssh_executable,
)

#: Where a hub keeps its repositories, relative to the hub user's home. Devices
#: use it as a remote path, so it stays in the ``host:path`` form Git accepts
#: without a scheme, which resolves against the remote home directory.
HUB_WORKSPACES_RELATIVE = ".guildbotics/hub/workspaces"
#: How long a host key probe waits before reporting the hub unreachable.
PROBE_TIMEOUT_SECONDS = 10.0
#: How long a hub command may take. Creating a repository is quick; the bound
#: exists so a hub that accepts the connection and then hangs still answers.
COMMAND_TIMEOUT_SECONDS = 30.0
#: The key type generated for a device that has none.
SSH_KEY_TYPE = "ed25519"

_HOST = re.compile(r"^[A-Za-z0-9._\-]+$")
_USER = re.compile(r"^[A-Za-z0-9._\-\\$]+$")


class InvalidHubEndpointError(ValueError):
    """Raised when text cannot name a hub machine."""


class HubUnreachableError(RuntimeError):
    """Raised when a hub could not be reached or refused a command."""


@dataclass(frozen=True)
class HubEndpoint:
    """Another machine hosting a hub, as OpenSSH addresses it.

    Attributes:
        host (str): The host name or address.
        user (str | None): The login name, or None to let SSH configuration
            decide -- which is what a user with an ``~/.ssh/config`` entry
            expects.
    """

    host: str
    user: str | None = None

    @property
    def target(self) -> str:
        """Return the ``[user@]host`` argument OpenSSH and Git both take."""
        return f"{self.user}@{self.host}" if self.user else self.host


@dataclass(frozen=True)
class HubLocation:
    """Where a hub lives as seen from this device.

    Attributes:
        endpoint (HubEndpoint | None): The machine hosting it, or None when
            this device is the hub machine and needs no network at all.
    """

    endpoint: HubEndpoint | None = None

    @property
    def is_local(self) -> bool:
        """True when the hub is this machine."""
        return self.endpoint is None

    @property
    def label(self) -> str:
        """Return how this location is named to the user."""
        return "this machine" if self.endpoint is None else self.endpoint.target


@dataclass(frozen=True)
class HubHostKey:
    """The identity a hub machine presents, for the user to confirm once.

    Attributes:
        fingerprints (tuple[str, ...]): The ``SHA256:`` fingerprints offered.
        trusted (bool): Whether this machine already knows the host.
    """

    fingerprints: tuple[str, ...]
    trusted: bool


@dataclass(frozen=True)
class HubSshKey:
    """This device's public key, for registration on a hub machine.

    Attributes:
        path (Path): The public key file.
        public_key (str): Its single line, as it goes into ``authorized_keys``.
        fingerprint (str): The ``SHA256:`` fingerprint of the same key.
    """

    path: Path
    public_key: str
    fingerprint: str


def parse_hub_endpoint(text: str) -> HubEndpoint:
    """Parse ``[user@]host`` into an endpoint.

    An ``ssh://`` prefix is accepted because it is what a user who copies a URL
    will paste. A port and a path are not: the hub's location inside the remote
    home is decided here, not by whoever types the endpoint.

    Raises:
        InvalidHubEndpointError: When the text names something else.
    """
    value = text.strip()
    if value.startswith("ssh://"):
        value = value[len("ssh://") :]
    if not value or any(character in value for character in "/ \t:"):
        raise InvalidHubEndpointError(
            f"{text!r} is not a hub address. Use user@host or host."
        )
    user, separator, hostname = value.rpartition("@")
    if not _HOST.match(hostname) or (separator and not _USER.match(user)):
        raise InvalidHubEndpointError(
            f"{text!r} is not a hub address. Use user@host or host."
        )
    return HubEndpoint(host=hostname, user=user or None)


def hub_remote_url(location: HubLocation, workspace_id: str) -> str:
    """Return the Git remote one workspace uses to reach its hub."""
    if location.endpoint is None:
        return str(host.workspace_repository_path(workspace_id))
    _require_uuid(workspace_id)
    return (
        f"{location.endpoint.target}:"
        f"{HUB_WORKSPACES_RELATIVE}/{workspace_id}/repository.git"
    )


def list_hub_workspaces(location: HubLocation) -> list[str]:
    """Return the workspaces a hub hosts, so the user can pick one to join."""
    if location.endpoint is None:
        return host.list_workspace_ids()
    output = _run_hub_command(
        location.endpoint, ["workspace", "list", "--format", "json"]
    )
    try:
        payload = json.loads(output)
        return [str(workspace_id) for workspace_id in payload["workspaces"]]
    except (ValueError, KeyError, TypeError) as exc:
        raise HubUnreachableError(
            f"{location.endpoint.target} did not answer with a workspace list. "
            "Check that GuildBotics is installed there and on the PATH used by "
            "SSH sessions."
        ) from exc


def create_hub_workspace(location: HubLocation, workspace_id: str) -> None:
    """Register a workspace on a hub, creating the repository devices push to."""
    _require_uuid(workspace_id)
    if location.endpoint is None:
        host.create_workspace_repository(workspace_id)
        return
    _run_hub_command(location.endpoint, ["workspace", "create", workspace_id])


def probe_host_key(endpoint: HubEndpoint) -> HubHostKey:
    """Read the host key a hub machine presents, without trusting it.

    The user confirms the fingerprint before anything is stored, which is the
    one moment where a machine-in-the-middle can be caught.
    """
    return HubHostKey(
        fingerprints=_fingerprints(_scan_host_keys(endpoint)),
        trusted=_is_known_host(endpoint),
    )


def trust_host_key(endpoint: HubEndpoint) -> HubHostKey:
    """Record a hub machine's host key in ``known_hosts``.

    Only call this once the user has confirmed the fingerprint from
    :func:`probe_host_key`.
    """
    lines = _scan_host_keys(endpoint)
    path = _known_hosts_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = (
        set(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else set()
    )
    added = [line for line in lines if line not in existing]
    if added:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(added) + "\n")
        path.chmod(0o600)
    return HubHostKey(fingerprints=_fingerprints(lines), trusted=True)


def read_ssh_key() -> HubSshKey | None:
    """Return this device's public key, or None when it has none yet."""
    path = _ssh_dir() / f"id_{SSH_KEY_TYPE}.pub"
    if not path.is_file():
        return None
    return _ssh_key(path)


def ensure_ssh_key() -> HubSshKey:
    """Return this device's public key, generating a key pair on first use.

    A key per device is what makes a lost machine revocable on its own: the
    user removes that one public key from the hub instead of rotating a secret
    every other device also holds.
    """
    existing = read_ssh_key()
    if existing is not None:
        return existing
    keygen = keygen_executable()
    if keygen is None:
        raise HubUnreachableError(
            "ssh-keygen was not found. Install OpenSSH to create a device key."
        )
    private = _ssh_dir() / f"id_{SSH_KEY_TYPE}"
    private.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _run(
        [keygen, "-t", SSH_KEY_TYPE, "-N", "", "-C", "guildbotics", "-f", str(private)],
        "generate an SSH key",
    )
    return _ssh_key(private.with_suffix(".pub"))


def _run_hub_command(endpoint: HubEndpoint, arguments: list[str]) -> str:
    """Run ``guildbotics hub ...`` on the hub machine over SSH.

    The hub machine runs GuildBotics itself, so the hub's own command is what
    creates its repositories -- this side never reaches in with raw Git.
    """
    command = [
        ssh_executable(),
        "-o",
        "BatchMode=yes",
        endpoint.target,
        "guildbotics",
        "hub",
        *arguments,
    ]
    return _run(command, f"run a hub command on {endpoint.target}")


def _scan_host_keys(endpoint: HubEndpoint) -> list[str]:
    keyscan = keyscan_executable()
    if keyscan is None:
        raise HubUnreachableError(
            "ssh-keyscan was not found. Install OpenSSH to connect to a hub."
        )
    output = _run(
        [keyscan, "-T", str(int(PROBE_TIMEOUT_SECONDS)), endpoint.host],
        f"read the host key of {endpoint.host}",
    )
    lines = [line for line in output.splitlines() if line and not line.startswith("#")]
    if not lines:
        raise HubUnreachableError(
            f"{endpoint.host} did not answer with an SSH host key."
        )
    return lines


def _fingerprints(lines: list[str]) -> tuple[str, ...]:
    keygen = keygen_executable()
    if keygen is None:
        raise HubUnreachableError(
            "ssh-keygen was not found. Install OpenSSH to connect to a hub."
        )
    output = _run(
        [keygen, "-l", "-f", "-"],
        "read an SSH host key fingerprint",
        stdin="\n".join(lines) + "\n",
    )
    return tuple(
        field
        for line in output.splitlines()
        for field in line.split()
        if field.startswith("SHA256:")
    )


def _is_known_host(endpoint: HubEndpoint) -> bool:
    keygen = keygen_executable()
    if keygen is None or not _known_hosts_path().is_file():
        return False
    result = subprocess.run(
        [keygen, "-F", endpoint.host],
        capture_output=True,
        text=True,
        check=False,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _ssh_key(public_path: Path) -> HubSshKey:
    keygen = keygen_executable()
    public_key = public_path.read_text(encoding="utf-8").strip()
    fingerprint = ""
    if keygen is not None:
        output = _run(
            [keygen, "-l", "-f", str(public_path)], "read an SSH key fingerprint"
        )
        fingerprint = next(
            (field for field in output.split() if field.startswith("SHA256:")), ""
        )
    return HubSshKey(path=public_path, public_key=public_key, fingerprint=fingerprint)


def _run(command: list[str], action: str, stdin: str | None = None) -> str:
    """Run one OpenSSH tool, reporting a failure as an unreachable hub."""
    try:
        result = subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HubUnreachableError(f"Could not {action}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise HubUnreachableError(
            f"Could not {action}: {detail[-1] if detail else 'no output'}"
        )
    return result.stdout


def _ssh_dir() -> Path:
    return Path.home() / ".ssh"


def _known_hosts_path() -> Path:
    return _ssh_dir() / "known_hosts"


def _require_uuid(workspace_id: str) -> None:
    """Refuse an identifier that could name something other than a workspace.

    It travels to a remote shell as a command argument and to a directory name,
    so it is checked before it can become either.
    """
    try:
        uuid.UUID(workspace_id)
    except ValueError as exc:
        raise InvalidHubEndpointError(
            f"{workspace_id!r} is not a workspace identifier."
        ) from exc
