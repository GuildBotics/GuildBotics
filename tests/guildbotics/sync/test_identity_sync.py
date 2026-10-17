"""The members' identities, synchronized between two machines through one hub.

Each machine is a home directory of its own -- its own identity root, its own
device -- and the hub is a directory on the first one, which the second
reaches by path. The identities use the workspaces' synchronization unchanged,
so what is checked here is that an identity saved on one machine is the one
the other machine has, in both directions, and that announcing a change is
what sends it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from git import Repo

from guildbotics.hub import host
from guildbotics.sync import activation
from guildbotics.sync.activation import (
    activate_identity_sync,
    commit_and_push_once,
    deactivate_identity_sync,
    synchronize_once,
)
from guildbotics.sync.enrollment import enroll
from guildbotics.utils.fileio import get_identity_root
from guildbotics.utils.person_id import IDENTITY_FILE, identity_members_dir
from guildbotics.utils.workspace_sync_port import ChangeSet, write_shared_text
from guildbotics.workspace.identity import ensure_workspace_identity


def _machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    """Be the machine ``name`` from now on, and return its identity root."""
    home = tmp_path / name
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return get_identity_root()


def _save(name: str) -> ChangeSet | None:
    """Save alice's identity on the machine this is, as the setup screen does."""
    path = identity_members_dir() / "alice" / IDENTITY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_shared_text(path, yaml.safe_dump({"person_id": "alice", "name": name}))


def _name() -> str:
    path = identity_members_dir() / "alice" / IDENTITY_FILE
    return str(yaml.safe_load(path.read_text(encoding="utf-8"))["name"])


@pytest.fixture
def hub_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """The identities' repository on a hub the first machine hosts, holding the
    first machine's identities."""
    mac = _machine(tmp_path, monkeypatch, "mac")
    host.create_hub()
    identity_id = host.claim_identity(ensure_workspace_identity(mac).workspace_id)
    _save("Alice")
    url = str(host.workspace_repository_path(identity_id))
    enroll(url, mac)
    return url


def test_an_identity_saved_on_one_machine_is_the_other_machines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hub_url: str
) -> None:
    windows = _machine(tmp_path, monkeypatch, "windows")
    enroll(hub_url, windows)
    assert _name() == "Alice"

    _save("Alice from Windows")
    commit_and_push_once(windows)
    mac = _machine(tmp_path, monkeypatch, "mac")
    synchronize_once(mac)

    assert _name() == "Alice from Windows"


def test_a_machine_joining_keeps_identities_only_it_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hub_url: str
) -> None:
    """Joining adopts the hub's identities and sends the others, as a
    workspace joining a hub's copy of itself does."""
    windows = _machine(tmp_path, monkeypatch, "windows")
    bob = identity_members_dir() / "bob" / IDENTITY_FILE
    bob.parent.mkdir(parents=True)
    write_shared_text(bob, "person_id: bob\nname: Bob\n")

    enroll(hub_url, windows)
    _machine(tmp_path, monkeypatch, "mac")
    synchronize_once(get_identity_root())

    assert _name() == "Alice"
    assert (identity_members_dir() / "bob" / IDENTITY_FILE).is_file()


def test_announcing_a_change_sends_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hub_url: str
) -> None:
    """No one commits the identities by hand: the port's announcement of a
    write is what has it committed and pushed."""
    windows = _machine(tmp_path, monkeypatch, "windows")
    enroll(hub_url, windows)
    _machine(tmp_path, monkeypatch, "mac")
    pushed = Repo(hub_url).head.commit
    activate_identity_sync()
    try:
        change = _save("Alice renamed on the Mac")
        assert change is not None
        manager = activation._identity_manager
        assert manager is not None
        # The queue's own barrier, which waits as long as the product does for
        # a change to be shared, rather than a deadline of this test's own.
        assert manager.await_pushed(change.change_id)
    finally:
        assert deactivate_identity_sync()
    assert Repo(hub_url).head.commit != pushed

    _machine(tmp_path, monkeypatch, "windows")
    synchronize_once(windows)

    assert _name() == "Alice renamed on the Mac"
