"""Hosting a hub: what the repositories accept, and what survives a retry."""

from __future__ import annotations

from pathlib import Path

import pytest
from git import GitCommandError, Repo

from guildbotics.hub import host

WORKSPACE_ID = "0198ab00-0000-7000-8000-000000000001"
OTHER_WORKSPACE_ID = "0198ab00-0000-7000-8000-000000000002"


def test_a_machine_hosts_no_hub_until_one_is_created(machine_root: Path) -> None:
    assert host.read_hub() is None
    assert host.list_workspace_ids() == []


def test_creating_a_hub_records_it_and_suggests_an_address(machine_root: Path) -> None:
    settings = host.create_hub()

    assert host.read_hub() == settings
    assert settings.ssh_endpoint
    assert (machine_root / "hub" / "hub.json").is_file()


def test_creating_a_hub_twice_keeps_the_same_identifier(machine_root: Path) -> None:
    first = host.create_hub()

    assert host.create_hub().hub_id == first.hub_id


def test_a_workspace_repository_is_created_on_the_shared_branch(
    machine_root: Path,
) -> None:
    host.create_hub()

    path = host.create_workspace_repository(WORKSPACE_ID)

    assert path == machine_root / "hub" / "workspaces" / WORKSPACE_ID / "repository.git"
    assert Repo(path).bare
    assert Repo(path).git.symbolic_ref("HEAD") == f"refs/heads/{host.HUB_BRANCH}"


def test_a_workspace_repository_refuses_a_force_push(
    machine_root: Path, tmp_path: Path
) -> None:
    host.create_hub()
    hub_path = host.create_workspace_repository(WORKSPACE_ID)
    device = _device_pushing_to(tmp_path / "device", hub_path)
    device.git.commit("--allow-empty", "-m", "second")
    device.git.push("origin", "main:main")

    # Rewind past the commit the hub now holds and build a different one, which
    # is the shape of every change that would drop another device's work.
    device.git.reset("--hard", "HEAD~1")
    device.git.commit("--allow-empty", "-m", "a different second")

    with pytest.raises(GitCommandError):
        device.git.push("--force", "origin", "main:main")


def test_a_workspace_repository_refuses_deleting_the_shared_branch(
    machine_root: Path, tmp_path: Path
) -> None:
    host.create_hub()
    hub_path = host.create_workspace_repository(WORKSPACE_ID)
    device = _device_pushing_to(tmp_path / "device", hub_path)

    with pytest.raises(GitCommandError):
        device.git.push("origin", ":main")


def test_creating_a_workspace_repository_again_keeps_its_content(
    machine_root: Path, tmp_path: Path
) -> None:
    host.create_hub()
    hub_path = host.create_workspace_repository(WORKSPACE_ID)
    _device_pushing_to(tmp_path / "device", hub_path)

    assert host.create_workspace_repository(WORKSPACE_ID) == hub_path
    assert Repo(hub_path).git.rev_parse("main")


def test_a_machine_that_is_not_a_hub_refuses_to_hold_a_workspace(
    machine_root: Path,
) -> None:
    with pytest.raises(host.HubNotHostedError):
        host.create_workspace_repository(WORKSPACE_ID)


def test_a_workspace_identifier_that_is_not_a_uuid_is_refused(
    machine_root: Path,
) -> None:
    host.create_hub()

    with pytest.raises(ValueError):
        host.create_workspace_repository("../../escape")


def test_listing_reports_every_hosted_workspace(machine_root: Path) -> None:
    host.create_hub()
    host.create_workspace_repository(OTHER_WORKSPACE_ID)
    host.create_workspace_repository(WORKSPACE_ID)

    assert host.list_workspace_ids() == [WORKSPACE_ID, OTHER_WORKSPACE_ID]


def _device_pushing_to(path: Path, hub_path: Path) -> Repo:
    """A repository with one commit already on the hub."""
    repository = Repo.init(path, initial_branch="main")
    repository.git.config("user.name", "GuildBotics")
    repository.git.config("user.email", "sync@guildbotics.invalid")
    repository.create_remote("origin", str(hub_path))
    repository.git.commit("--allow-empty", "-m", "first")
    repository.git.push("origin", "main:main")
    return repository
