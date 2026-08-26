"""The hub as the place values are held, and the race it settles.

The hub has no idea what a workspace contains, so the only agreement it can
check is with itself: a device says which generation it believes the hub holds,
and anything else is refused rather than overwritten.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from guildbotics.hub import host, secret_host


@pytest.fixture
def workspace_id(machine_root: Path) -> str:
    del machine_root
    host.create_hub()
    identifier = "11111111-2222-3333-4444-555555555555"
    host.create_workspace_repository(identifier)
    return identifier


def _store(workspace_id: str, key: str, base: int, candidate: int, value: str):
    return secret_host.store_secret(
        workspace_id,
        key,
        base_generation=base,
        candidate_generation=candidate,
        value=value,
    )


def test_a_value_is_held_and_handed_back(fake_keyring, workspace_id: str):
    assert _store(workspace_id, "A_TOKEN", 0, 1, "ghp-first") == 1

    assert secret_host.read_secret(workspace_id, "A_TOKEN") == ("ghp-first", 1)
    assert secret_host.generations(workspace_id)["A_TOKEN"] == 1


def test_no_value_reaches_the_hub_repository(fake_keyring, workspace_id: str):
    """The bare repository is the one thing every device fetches. A value that
    landed in it would be in the shared history on every machine forever."""
    _store(workspace_id, "A_TOKEN", 0, 1, "ghp-first")

    written = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in host.hub_root().rglob("*")
        if path.is_file()
    )

    assert "ghp-first" not in written


def test_a_second_device_from_the_same_base_is_refused(fake_keyring, workspace_id: str):
    _store(workspace_id, "A_TOKEN", 0, 1, "from-mac")

    with pytest.raises(secret_host.HubSecretConflictError):
        _store(workspace_id, "A_TOKEN", 0, 1, "from-windows")

    assert secret_host.read_secret(workspace_id, "A_TOKEN") == ("from-mac", 1)


def test_sending_past_what_the_hub_holds_is_accepted(fake_keyring, workspace_id: str):
    """How a send cut off before its generation was recorded is settled: the
    next sender builds on what the hub holds and moves past it."""
    _store(workspace_id, "A_TOKEN", 0, 1, "unrecorded")

    assert _store(workspace_id, "A_TOKEN", 1, 2, "settled") == 2
    assert secret_host.read_secret(workspace_id, "A_TOKEN") == ("settled", 2)


def test_the_generation_is_recorded_before_the_value(fake_keyring, workspace_id: str):
    """An interruption between the hub's two writes must not leave it serving a
    new value under an older generation's name.

    That state is invisible: every device would fetch the value as the one they
    agreed on. Recording the generation first turns the same interruption into
    the state the design already handles -- a generation no device fetches, and
    which the next send moves past."""
    written: list[str] = []

    class _Recording:
        def validate_password(self, username: str, password: str) -> None:
            del username, password

        def get_password(self, service: str, username: str) -> str | None:
            del service, username
            return None

        def set_password(self, service: str, username: str, password: str) -> None:
            del service, password
            written.append(username)

        def delete_password(self, service: str, username: str) -> None:
            del service, username

    _store(workspace_id, "A_TOKEN", 0, 1, "first")
    generations = (
        host.workspace_repository_path(workspace_id).parent
        / secret_host.GENERATIONS_FILENAME
    )
    recorded_before_write = []

    class _Watching(_Recording):
        def set_password(self, service: str, username: str, password: str) -> None:
            recorded_before_write.append(
                json.loads(generations.read_text(encoding="utf-8"))["keys"]["A_TOKEN"]
            )
            super().set_password(service, username, password)

    secret_host.store_secret(
        workspace_id,
        "A_TOKEN",
        base_generation=1,
        candidate_generation=2,
        value="second",
        keychain=_Watching(),
    )

    assert recorded_before_write == [2]


def test_a_hub_behind_the_workspace_still_accepts_a_value(
    fake_keyring, workspace_id: str
):
    """A hub restored from an older copy holds a value no device can fetch, so
    refusing to send past it would leave the workspace no way to put one back."""
    _store(workspace_id, "A_TOKEN", 0, 1, "older")

    assert _store(workspace_id, "A_TOKEN", 4, 5, "current") == 5
    assert secret_host.read_secret(workspace_id, "A_TOKEN") == ("current", 5)


def test_a_rebuilt_hub_accepts_whatever_generation_the_workspace_reached(
    fake_keyring, workspace_id: str
):
    """Values cannot be restored from the shared history, so the first device
    to send one after a rebuild puts it back at the generation it is on."""
    assert _store(workspace_id, "A_TOKEN", 6, 7, "restored") == 7


def test_a_generation_that_is_not_the_next_one_is_refused(
    fake_keyring, workspace_id: str
):
    with pytest.raises(secret_host.HubSecretError):
        _store(workspace_id, "A_TOKEN", 1, 5, "skipped")


def test_an_unknown_key_is_reported_rather_than_answered(
    fake_keyring, workspace_id: str
):
    with pytest.raises(secret_host.HubSecretMissingError):
        secret_host.read_secret(workspace_id, "NEVER_SENT")


def test_a_recorded_key_whose_value_vanished_is_reported(
    fake_keyring, workspace_id: str
):
    _store(workspace_id, "A_TOKEN", 0, 1, "from-mac")
    fake_keyring.passwords.clear()

    with pytest.raises(secret_host.HubSecretMissingError):
        secret_host.read_secret(workspace_id, "A_TOKEN")


@pytest.mark.parametrize("key", ["", "with space", "../escape", "a-dash", "semi;colon"])
def test_a_key_that_is_not_a_name_is_refused(fake_keyring, workspace_id: str, key: str):
    with pytest.raises(secret_host.InvalidSecretKeyError):
        secret_host.read_secret(workspace_id, key)


def test_a_workspace_this_hub_never_registered_is_refused(
    fake_keyring, machine_root: Path
):
    """Otherwise any reachable device could name a directory into existence."""
    del machine_root
    host.create_hub()

    with pytest.raises(secret_host.HubSecretError):
        secret_host.generations("11111111-2222-3333-4444-555555555555")


def test_a_machine_that_hosts_no_hub_refuses(fake_keyring, machine_root: Path):
    del machine_root

    with pytest.raises(host.HubNotHostedError):
        secret_host.generations("11111111-2222-3333-4444-555555555555")


def test_the_generations_file_records_no_value(fake_keyring, workspace_id: str):
    _store(workspace_id, "A_TOKEN", 0, 1, "ghp-first")

    payload = json.loads(
        (
            host.workspace_repository_path(workspace_id).parent
            / secret_host.GENERATIONS_FILENAME
        ).read_text(encoding="utf-8")
    )

    assert payload["keys"] == {"A_TOKEN": 1}
