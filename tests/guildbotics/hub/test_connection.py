"""Reaching a hub: what counts as an address, and where a workspace pushes."""

from __future__ import annotations

import json
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
