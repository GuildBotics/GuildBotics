"""Reaching a hub: what counts as an address, and where a workspace pushes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from guildbotics.hub import connection, host
from guildbotics.hub.connection import (
    HubEndpoint,
    HubLocation,
    HubUnreachableError,
    InvalidHubEndpointError,
)

WORKSPACE_ID = "0198ab00-0000-7000-8000-000000000001"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hub.local", HubEndpoint(host="hub.local")),
        ("me@hub.local", HubEndpoint(host="hub.local", user="me")),
        ("  me@hub.local  ", HubEndpoint(host="hub.local", user="me")),
        ("ssh://me@hub.local", HubEndpoint(host="hub.local", user="me")),
    ],
)
def test_an_address_names_a_machine_and_optionally_a_login(
    text: str, expected: HubEndpoint
) -> None:
    assert connection.parse_hub_endpoint(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "me@hub.local:2222",
        "me@hub.local/workspaces",
        "me@hub local",
        "@hub.local",
        "me@",
    ],
)
def test_anything_that_is_not_an_address_is_refused(text: str) -> None:
    """Where a hub keeps its repositories is decided here, not by the user."""
    with pytest.raises(InvalidHubEndpointError):
        connection.parse_hub_endpoint(text)


def test_a_remote_hub_is_reached_at_a_path_inside_its_home() -> None:
    location = HubLocation(endpoint=HubEndpoint(host="hub.local", user="me"))

    url = connection.hub_remote_url(location, WORKSPACE_ID)

    assert url == (
        f"me@hub.local:.guildbotics/hub/workspaces/{WORKSPACE_ID}/repository.git"
    )


def test_a_local_hub_is_reached_by_filesystem_path(machine_root: Path) -> None:
    url = connection.hub_remote_url(HubLocation(), WORKSPACE_ID)

    assert Path(url) == host.workspace_repository_path(WORKSPACE_ID)


@pytest.mark.parametrize(
    "workspace_id",
    [
        "../../etc",
        "not-a-uuid",
        # Same UUID, non-canonical spellings: accepting them would spread one
        # workspace over several directories, and ``:`` is not a legal
        # directory name on a Windows hub.
        "urn:uuid:0198ab00-0000-7000-8000-000000000001",
        "{0198ab00-0000-7000-8000-000000000001}",
        "0198ab0000007000800000000000000001",
        "0198AB00-0000-7000-8000-000000000001",
    ],
)
def test_a_workspace_identifier_never_becomes_part_of_a_remote_command(
    workspace_id: str,
) -> None:
    location = HubLocation(endpoint=HubEndpoint(host="hub.local"))

    with pytest.raises(host.InvalidWorkspaceIdError):
        connection.hub_remote_url(location, workspace_id)


def test_a_local_hub_lists_the_workspaces_it_holds(machine_root: Path) -> None:
    host.create_hub()
    host.create_workspace_repository(WORKSPACE_ID)

    assert connection.list_hub_workspaces(HubLocation()) == [WORKSPACE_ID]


def test_a_remote_hub_lists_the_workspaces_its_own_command_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        connection,
        "run_hub_command",
        lambda endpoint, arguments: json.dumps({"workspaces": [WORKSPACE_ID]}),
    )
    location = HubLocation(endpoint=HubEndpoint(host="hub.local"))

    assert connection.list_hub_workspaces(location) == [WORKSPACE_ID]


def test_a_machine_without_guildbotics_is_reported_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shell answering something else is the PATH problem, not a bad hub."""
    monkeypatch.setattr(
        connection,
        "run_hub_command",
        lambda endpoint, arguments: "guildbotics: command not found\n",
    )
    location = HubLocation(endpoint=HubEndpoint(host="hub.local"))

    with pytest.raises(HubUnreachableError, match="PATH"):
        connection.list_hub_workspaces(location)


def test_a_local_hub_registers_a_workspace_without_any_connection(
    machine_root: Path,
) -> None:
    host.create_hub()

    connection.create_hub_workspace(HubLocation(), WORKSPACE_ID)

    assert host.list_workspace_ids() == [WORKSPACE_ID]


@pytest.mark.parametrize("text", ["-oProxyCommand=x", "-hub.local", "me@-hub.local"])
def test_an_address_that_would_reach_ssh_as_an_option_is_refused(text: str) -> None:
    with pytest.raises(InvalidHubEndpointError):
        connection.parse_hub_endpoint(text)


@pytest.mark.parametrize("text", ["DOMAIN\\me@hub.local", "machine$@hub.local"])
def test_a_windows_login_name_is_still_an_address(text: str) -> None:
    """``DOMAIN\\user`` and a machine account's trailing ``$`` are ordinary
    there, and they never reach a shell: every command is a list."""
    assert connection.parse_hub_endpoint(text).host == "hub.local"


ED25519 = "hub.local ssh-ed25519 AAAAC3Nz"
RSA = "hub.local ssh-rsa AAAAB3Nz"


def _offering(monkeypatch: pytest.MonkeyPatch, keys: dict[str, str]) -> None:
    """Make the hub offer these ``line -> fingerprint`` pairs."""
    monkeypatch.setattr(connection, "_scan_host_keys", lambda endpoint: list(keys))
    monkeypatch.setattr(connection, "_fingerprint_of", lambda line: keys[line])


def _stored(monkeypatch: pytest.MonkeyPatch, keys: set[tuple[str, str]]) -> None:
    """Make ``known_hosts`` already hold these ``(type, key)`` pairs."""
    monkeypatch.setattr(connection, "_known_host_keys", lambda endpoint: keys)


def test_a_host_offering_a_key_this_device_stored_is_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _offering(monkeypatch, {ED25519: "SHA256:stored"})
    _stored(monkeypatch, {("ssh-ed25519", "AAAAC3Nz")})

    result = connection.probe_host_key(HubEndpoint(host="hub.local"))

    assert (result.trusted, result.changed) == (True, False)


def test_a_host_offering_a_key_other_than_the_stored_one_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Knowing the host by name is not enough. Every later connection is checked
    against the stored key, so calling this trusted sends the caller straight
    into an SSH call that fails on the host key -- and the fingerprints the user
    has to compare would never be shown."""
    _offering(monkeypatch, {ED25519: "SHA256:rotated"})
    _stored(monkeypatch, {("ssh-ed25519", "AAAAreplaced")})

    result = connection.probe_host_key(HubEndpoint(host="hub.local"))

    assert (result.trusted, result.changed) == (False, True)
    assert result.fingerprints == ("SHA256:rotated",)


def test_a_host_no_key_is_stored_for_is_a_first_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _offering(monkeypatch, {ED25519: "SHA256:new"})
    _stored(monkeypatch, set())

    result = connection.probe_host_key(HubEndpoint(host="hub.local"))

    assert (result.trusted, result.changed) == (False, False)


def _connecting(
    monkeypatch: pytest.MonkeyPatch, presented: str | None, stderr: str = ""
) -> list[list[str]]:
    """Answer the probe's ``ssh`` call, writing what the machine presented.

    A real client records the key during key exchange and then fails on
    authentication, so the exit status is non-zero while the file is written.
    """
    calls: list[list[str]] = []

    def _ssh(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(command)
        store = Path(_option(command, "UserKnownHostsFile").strip('"'))
        if presented is not None:
            store.write_text(f"{presented}\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 255, "", stderr)

    monkeypatch.setattr(connection.subprocess, "run", _ssh)
    return calls


def _option(command: list[str], name: str) -> str:
    """Return the value of one ``-o name=value`` in a command."""
    return next(
        argument.split("=", 1)[1]
        for argument in command
        if argument.startswith(f"{name}=")
    )


def test_the_host_key_is_read_before_this_device_is_let_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key is recorded during key exchange, so a device whose public key is
    not registered on the hub yet still gets a fingerprint to confirm. Treating
    the refused login as a failed probe would leave the user no way to start."""
    calls = _connecting(monkeypatch, ED25519, stderr="Permission denied (publickey).")

    assert connection._scan_host_keys(HubEndpoint(host="hub.local")) == [ED25519]
    assert calls[0][-2:] == ["hub.local", "exit"]


def test_the_probe_never_reads_the_keys_this_device_already_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`accept-new` records only what is unknown, so a `known_hosts` holding the
    host would leave the probe with nothing to report -- and a stored key read
    back as though the machine had just offered it would confirm itself."""
    calls = _connecting(monkeypatch, ED25519)

    connection._scan_host_keys(HubEndpoint(host="hub.local", user="me"))

    for name in ("UserKnownHostsFile", "GlobalKnownHostsFile"):
        assert _option(calls[0], name).strip('"') not in (
            str(connection._known_hosts_path()),
            "/etc/ssh/ssh_known_hosts",
        )
    assert calls[0][-2] == "me@hub.local"


def test_a_machine_that_offered_no_host_key_reports_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason belongs in the message: "could not be reached" alone covers a
    name that does not resolve, a refused connection, and a client that cannot
    negotiate, which are three different things to do next."""
    _connecting(
        monkeypatch,
        None,
        stderr="ssh: connect to host hub.local port 22: Connection timed out",
    )

    with pytest.raises(HubUnreachableError, match="Connection timed out"):
        connection._scan_host_keys(HubEndpoint(host="hub.local"))


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("hub.local ssh-ed25519 AAAAC3Nz", ("ssh-ed25519", "AAAAC3Nz")),
        ("|1|aGFzaA==|aGFzaA== ssh-ed25519 AAAAC3Nz", ("ssh-ed25519", "AAAAC3Nz")),
        ("@cert-authority hub.local ssh-ed25519 AAAAC3Nz", None),
        ("@revoked hub.local ssh-ed25519 AAAAC3Nz", None),
        ("# Host hub.local found: line 3", None),
        ("", None),
    ],
)
def test_the_comparable_part_of_a_host_key_line(
    line: str, expected: tuple[str, str] | None
) -> None:
    """``ssh-keygen -F`` names the host by hash and ``ssh-keyscan`` names it
    plainly, so only the type and the key itself compare between them. A marked
    line is not a plain host key at all, and reading one as though the marker
    were absent would claim a trust OpenSSH does not grant."""
    assert connection._key_material(line) == expected


def test_a_revoked_key_the_hub_offers_is_never_trusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`@revoked` records a key the user must not connect with.

    Counting it as a stored key would skip the confirmation this probe exists
    for, and the connection would then be refused on that very key -- the same
    dead end as an unrecognized rotation.
    """
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(f"@revoked {ED25519}\n", encoding="utf-8")
    monkeypatch.setattr(connection, "_known_hosts_path", lambda: known_hosts)
    monkeypatch.setattr(connection, "keygen_executable", lambda: "ssh-keygen")
    monkeypatch.setattr(
        connection.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, known_hosts.read_text(encoding="utf-8"), ""
        ),
    )
    _offering(monkeypatch, {ED25519: "SHA256:revoked"})

    result = connection.probe_host_key(HubEndpoint(host="hub.local"))

    assert result.trusted is False
    assert result.fingerprints == ("SHA256:revoked",)


def test_trusting_stores_the_key_the_user_confirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(connection, "_ssh_dir", lambda: tmp_path / ".ssh")
    _offering(monkeypatch, {ED25519: "SHA256:confirmed"})

    result = connection.trust_host_key(
        HubEndpoint(host="hub.local"), "SHA256:confirmed"
    )

    assert result.trusted is True
    assert (tmp_path / ".ssh" / "known_hosts").read_text().strip() == ED25519


def test_a_machine_offering_a_different_key_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The confirmation is the only moment a machine-in-the-middle is caught, so
    storing whatever the second read returns would make it decorative."""
    monkeypatch.setattr(connection, "_ssh_dir", lambda: tmp_path / ".ssh")
    _offering(monkeypatch, {ED25519: "SHA256:other"})

    with pytest.raises(connection.HostKeyChangedError):
        connection.trust_host_key(HubEndpoint(host="hub.local"), "SHA256:confirmed")

    assert not (tmp_path / ".ssh" / "known_hosts").exists()


def test_only_the_confirmed_key_is_trusted_when_several_are_offered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A confirmed public key is public, so anyone can present it again. Keys
    that merely arrive alongside it were never shown to the user, and a later
    connection could be negotiated onto one of them."""
    monkeypatch.setattr(connection, "_ssh_dir", lambda: tmp_path / ".ssh")
    _offering(monkeypatch, {ED25519: "SHA256:confirmed", RSA: "SHA256:unseen"})

    result = connection.trust_host_key(
        HubEndpoint(host="hub.local"), "SHA256:confirmed"
    )

    known_hosts = (tmp_path / ".ssh" / "known_hosts").read_text()
    assert ED25519 in known_hosts
    assert "ssh-rsa" not in known_hosts
    assert result.fingerprints == ("SHA256:confirmed",)


def test_a_generated_key_names_the_machine_in_its_comment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Revoking a lost device means deleting its line from the hub's
    ``authorized_keys``, and the comment is the only part of that line a person
    reads. A constant would leave every device's line identical, with nothing
    but fingerprints to tell them apart."""
    ssh_dir = tmp_path / ".ssh"
    monkeypatch.setattr(connection, "_ssh_dir", lambda: ssh_dir)
    monkeypatch.setattr(connection, "keygen_executable", lambda: "ssh-keygen")
    monkeypatch.setattr(host, "default_ssh_endpoint", lambda: "alice@mac-studio")
    commands: list[list[str]] = []

    def _keygen(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        commands.append(command)
        if "-t" in command:
            (ssh_dir / f"id_{connection.SSH_KEY_TYPE}.pub").write_text(
                "ssh-ed25519 AAAAC3Nz guildbotics alice@mac-studio\n", encoding="utf-8"
            )
        return subprocess.CompletedProcess(command, 0, "256 SHA256:device key", "")

    monkeypatch.setattr(connection.subprocess, "run", _keygen)

    key = connection.ensure_ssh_key()

    generated = commands[0]
    assert generated[generated.index("-C") + 1] == "guildbotics alice@mac-studio"
    assert key.fingerprint == "SHA256:device"
