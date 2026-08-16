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
        "_run_hub_command",
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
        "_run_hub_command",
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
