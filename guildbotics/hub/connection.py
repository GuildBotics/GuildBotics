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
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from guildbotics.hub import host
from guildbotics.utils.openssh import (
    REMOTE_COMMAND_TIMEOUT_SECONDS,
    keygen_executable,
    ssh_executable,
)

#: Where a hub keeps its repositories, relative to the hub user's home. Devices
#: use it as a remote path, so it stays in the ``host:path`` form Git accepts
#: without a scheme, which resolves against the remote home directory.
HUB_WORKSPACES_RELATIVE = ".guildbotics/hub/workspaces"
_HUB_REMOTE_PATH_PARTS = 2
#: How long a host key probe waits before reporting the hub unreachable.
PROBE_TIMEOUT_SECONDS = 10.0
#: How long a hub command may take. Creating a repository is quick; the bound
#: exists so a hub that accepts the connection and then hangs still answers.
COMMAND_TIMEOUT_SECONDS = REMOTE_COMMAND_TIMEOUT_SECONDS
#: The key type generated for a device that has none.
SSH_KEY_TYPE = "ed25519"
#: ``host keytype key`` is the shortest line either OpenSSH tool prints.
_HOST_KEY_FIELDS = 3

#: A leading ``-`` would reach ``ssh`` as an option rather than as the machine
#: to contact, so neither part may start with one.
_HOST = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._\-]*$")
#: Backslash and ``$`` are here for Windows: ``DOMAIN\user`` and the trailing
#: ``$`` of a machine account are ordinary login names there.
_USER = re.compile(r"^[A-Za-z0-9._\\$][A-Za-z0-9._\-\\$]*$")


class InvalidHubEndpointError(ValueError):
    """Raised when text cannot name a hub machine."""


class HubUnreachableError(RuntimeError):
    """Raised when a hub could not be reached or refused a command."""


class HostKeyChangedError(RuntimeError):
    """Raised when a hub offers a host key other than the one confirmed.

    Either the machine's key was replaced, or something else is answering for
    it. Both need the user to look again, so neither is trusted here.
    """


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
        trusted (bool): Whether one of the keys offered is already stored for
            this host. Knowing the host by name is not enough: the stored key
            is what every later connection is checked against.
        changed (bool): Whether keys are stored for this host but none of them
            is being offered. Either the machine was rebuilt or something else
            is answering for it, and only the user can tell which.
    """

    fingerprints: tuple[str, ...]
    trusted: bool
    changed: bool = False


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
    host.require_workspace_id(workspace_id)
    return (
        f"{location.endpoint.target}:"
        f"{HUB_WORKSPACES_RELATIVE}/{workspace_id}/repository.git"
    )


def location_from_remote_url(
    remote_url: str, workspace_id: str | None = None
) -> HubLocation:
    """Recover the Hub location from a validated synchronization remote."""
    return _parse_remote_url(remote_url, workspace_id)[0]


def workspace_id_from_remote_url(remote_url: str) -> str:
    """Return the workspace a synchronization remote names.

    The remote is the one record of which workspace on which machine this one
    belongs to, and both halves are read from it here rather than by whoever
    happens to need one of them.
    """
    return _parse_remote_url(remote_url, None)[1]


def _parse_remote_url(
    remote_url: str, workspace_id: str | None
) -> tuple[HubLocation, str]:
    value = remote_url.strip()
    if not value:
        raise InvalidHubEndpointError("The synchronization remote is empty.")
    if value.startswith("/") or value.startswith("./") or value.startswith("../"):
        return HubLocation(), _local_workspace_id(value)
    target, separator, path = value.partition(":")
    if not separator or not path.startswith(f"{HUB_WORKSPACES_RELATIVE}/"):
        raise InvalidHubEndpointError(
            f"{remote_url!r} is not a GuildBotics Hub remote."
        )
    relative = path.removeprefix(f"{HUB_WORKSPACES_RELATIVE}/")
    parts = relative.split("/")
    if len(parts) != _HUB_REMOTE_PATH_PARTS or parts[1] != "repository.git":
        raise InvalidHubEndpointError(
            f"{remote_url!r} is not a GuildBotics Hub remote."
        )
    try:
        remote_workspace_id = host.require_workspace_id(parts[0])
    except host.InvalidWorkspaceIdError as exc:
        raise InvalidHubEndpointError(
            f"{remote_url!r} contains an invalid workspace identifier."
        ) from exc
    if workspace_id is not None and remote_workspace_id != host.require_workspace_id(
        workspace_id
    ):
        raise InvalidHubEndpointError(
            f"{remote_url!r} names a different workspace than {workspace_id}."
        )
    return HubLocation(endpoint=parse_hub_endpoint(target)), remote_workspace_id


def _local_workspace_id(path: str) -> str:
    """Read the workspace out of a hub repository path on this machine."""
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if len(parts) < _HUB_REMOTE_PATH_PARTS or parts[-1] != "repository.git":
        raise InvalidHubEndpointError(f"{path!r} is not a GuildBotics Hub remote.")
    try:
        return host.require_workspace_id(parts[-2])
    except host.InvalidWorkspaceIdError as exc:
        raise InvalidHubEndpointError(
            f"{path!r} contains an invalid workspace identifier."
        ) from exc


def list_hub_workspaces(location: HubLocation) -> list[str]:
    """Return the workspaces a hub hosts, so the user can pick one to join."""
    if location.endpoint is None:
        return host.list_workspace_ids()
    output = run_hub_command(
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
    host.require_workspace_id(workspace_id)
    if location.endpoint is None:
        host.create_workspace_repository(workspace_id)
        return
    run_hub_command(location.endpoint, ["workspace", "create", workspace_id])


def probe_host_key(endpoint: HubEndpoint) -> HubHostKey:
    """Read the host key a hub machine presents, without trusting it.

    The user confirms the fingerprint before anything is stored, which is the
    one moment where a machine-in-the-middle can be caught.

    What is stored decides, not whether the host is named in ``known_hosts`` at
    all. A machine whose key was replaced is known by name and still cannot be
    connected to, so reporting it as trusted would send the caller on to an SSH
    call that fails on the host key -- and the fingerprints the user has to
    look at would never be shown.

    Trust here only ever means "a plain entry for this host holds one of the
    keys being offered". It is the weaker of the two claims on purpose: the
    connection itself is where OpenSSH decides, and this side is worth having
    only while it errs towards asking the user.
    """
    offered = _scan_host_keys(endpoint)
    stored = _known_host_keys(endpoint)
    recognized = {
        key
        for line in offered
        if (key := _key_material(line)) is not None and key in stored
    }
    return HubHostKey(
        fingerprints=tuple(_fingerprint_of(line) for line in offered),
        trusted=bool(recognized),
        changed=bool(stored) and not recognized,
    )


def trust_host_key(endpoint: HubEndpoint, fingerprint: str) -> HubHostKey:
    """Record in ``known_hosts`` the host key the user confirmed.

    The key is read again here rather than carried over from
    :func:`probe_host_key`, so the fingerprint the user actually looked at has
    to be named: storing whatever the second read returns would make the
    confirmation decorative, since a machine that answered honestly the first
    time could answer differently the second.

    Only the key with that fingerprint is stored, not everything the machine
    offers alongside it. A confirmed public key is public, so anyone can present
    it again; trusting the other keys that arrive with it would trust keys the
    user never saw, and a later connection could be negotiated onto one of them.

    Args:
        endpoint (HubEndpoint): The hub machine.
        fingerprint (str): The ``SHA256:`` fingerprint the user confirmed.

    Raises:
        HostKeyChangedError: When the machine no longer offers that key.
    """
    confirmed = [
        line
        for line in _scan_host_keys(endpoint)
        if _fingerprint_of(line) == fingerprint
    ]
    if not confirmed:
        raise HostKeyChangedError(
            f"{endpoint.host} no longer offers the host key that was confirmed. "
            "Check the fingerprint again before trusting it."
        )
    path = _known_hosts_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = (
        set(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else set()
    )
    added = [line for line in confirmed if line not in existing]
    if added:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(added) + "\n")
        path.chmod(0o600)
    return HubHostKey(fingerprints=(fingerprint,), trusted=True)


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
    every other device also holds. The comment names the machine, because it
    is the only readable part of an ``authorized_keys`` line and deleting the
    right line is the whole of revoking. A constant would leave every device's
    line identical, and the user matching fingerprints by hand.
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
        [
            keygen,
            "-t",
            SSH_KEY_TYPE,
            "-N",
            "",
            "-C",
            f"guildbotics {host.default_ssh_endpoint()}",
            "-f",
            str(private),
        ],
        "generate an SSH key",
    )
    return _ssh_key(private.with_suffix(".pub"))


def run_hub_command(endpoint: HubEndpoint, arguments: list[str]) -> str:
    """Run ``guildbotics hub ...`` on the hub machine over SSH.

    The hub machine runs GuildBotics itself, so the hub's own command is what
    creates its repositories -- this side never reaches in with raw Git.
    """
    return _run(
        hub_ssh_command(endpoint, arguments),
        f"run a hub command on {endpoint.target}",
    )


def run_hub_stream(
    endpoint: HubEndpoint, arguments: list[str], payload: bytes = b""
) -> bytes:
    """Run a hub command over SSH, exchanging raw bytes with it.

    Used where the exchange is a secret value rather than text: the bytes are
    handed over exactly as they are, with no newline translation and no
    decoding, and the command's own output never reaches an error message --
    only the standard error stream does, which carries diagnostics and never a
    value.
    """
    command = hub_ssh_command(endpoint, arguments)
    try:
        result = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HubUnreachableError(f"Could not reach {endpoint.target}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise HubUnreachableError(
            f"{endpoint.target} refused a hub command: "
            f"{detail[-1] if detail else 'no output'}"
        )
    return result.stdout


def hub_ssh_command(endpoint: HubEndpoint, arguments: list[str]) -> list[str]:
    """Build the OpenSSH argv used for a remote Hub CLI command."""
    return [
        ssh_executable(),
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        endpoint.target,
        "guildbotics",
        "hub",
        *arguments,
    ]


def _scan_host_keys(endpoint: HubEndpoint) -> list[str]:
    """Return the host key a hub presents, read by the client that will use it.

    ``ssh-keyscan`` is the tool named for this and cannot do it everywhere: the
    Windows build proposes key exchange algorithms it has not implemented, so
    against an OpenSSH 9 server it agrees on one and then aborts, while the very
    next ``ssh`` to the same machine succeeds. It has no option to narrow the
    proposal either, so there is nothing to pass it. Reading the key with
    ``ssh`` removes the asymmetry instead of working around it: the probe now
    sees what the connection will see, ``~/.ssh/config`` included, which
    ``ssh-keyscan`` never read.

    The key arrives through a ``known_hosts`` of our own that starts empty, so
    ``accept-new`` records exactly what this machine presented and nothing that
    was already stored. It is written during key exchange, before
    authentication, which is why a device whose public key is not registered on
    the hub yet still gets a fingerprint to confirm -- and why the exit status
    is ignored here. What the connection proves is which key was offered; being
    let in is a later question, asked by the hub commands.
    """
    with tempfile.TemporaryDirectory(prefix="guildbotics-hostkey-") as directory:
        store = Path(directory) / "known_hosts"
        command = [
            ssh_executable(),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            # Quoted because the value reaches OpenSSH's own configuration
            # parser, which splits on whitespace: the temporary directory is
            # under the user's profile on Windows and can hold spaces.
            "-o",
            f'UserKnownHostsFile="{store}"',
            "-o",
            f'GlobalKnownHostsFile="{Path(directory) / "global_known_hosts"}"',
            "-o",
            "HashKnownHosts=no",
            "-o",
            f"ConnectTimeout={int(PROBE_TIMEOUT_SECONDS)}",
            endpoint.target,
            "exit",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                stdin=subprocess.DEVNULL,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HubUnreachableError(
                f"Could not read the host key of {endpoint.host}: {exc}"
            ) from exc
        offered = store.read_text(encoding="utf-8") if store.is_file() else ""
    lines = [line for line in offered.splitlines() if line and not line.startswith("#")]
    if not lines:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise HubUnreachableError(
            f"Could not read the host key of {endpoint.host}: "
            f"{detail[-1] if detail else 'no host key was offered'}"
        )
    return lines


def _fingerprint_of(line: str) -> str:
    """Return the fingerprint of one scanned host key.

    One key at a time, so a fingerprint is never attributed to the wrong line:
    which key the user confirmed is the whole question here.
    """
    keygen = keygen_executable()
    if keygen is None:
        raise HubUnreachableError(
            "ssh-keygen was not found. Install OpenSSH to connect to a hub."
        )
    output = _run(
        [keygen, "-l", "-f", "-"],
        "read an SSH host key fingerprint",
        stdin=f"{line}\n",
    )
    return next((field for field in output.split() if field.startswith("SHA256:")), "")


def _known_host_keys(endpoint: HubEndpoint) -> set[tuple[str, str]]:
    """The keys already stored for this host, as ``(type, key)`` pairs."""
    keygen = keygen_executable()
    if keygen is None or not _known_hosts_path().is_file():
        return set()
    result = subprocess.run(
        [keygen, "-F", endpoint.host],
        capture_output=True,
        text=True,
        check=False,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return set()
    return {
        key
        for line in result.stdout.splitlines()
        if (key := _key_material(line)) is not None
    }


def _key_material(line: str) -> tuple[str, str] | None:
    """The ``(type, key)`` of one plain host key line, if it is one.

    A stored line names the host by hash and a scanned one names it plainly, so
    only the two fields after the name are comparable between them.

    A line carrying a marker -- ``@revoked``, ``@cert-authority`` -- is not one
    of these, and is dropped rather than read as though the marker were absent.
    What each marker means to OpenSSH is OpenSSH's to decide, and it decides it
    on the connection itself; reproducing that here would be claiming more than
    this comparison can know. Dropping them only ever costs a fingerprint the
    user is asked to look at again.
    """
    fields = line.split()
    if len(fields) < _HOST_KEY_FIELDS or fields[0].startswith(("#", "@")):
        return None
    return fields[1], fields[2]


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
