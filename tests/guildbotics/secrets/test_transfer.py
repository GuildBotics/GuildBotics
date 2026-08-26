"""Sending and fetching, against a hub held in memory.

The transfers are the only way a value moves between machines, so what is
checked here is the order of the two writes: the hub takes the value first, and
the generation that names it is published afterwards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from guildbotics.secrets import (
    SecretTransfer,
    bulk_fetch_keys,
    bulk_send_keys,
    is_unconfirmed,
    transfer_status,
)
from guildbotics.secrets.hub_client import (
    HUB_CONFLICT,
    HUB_LOCKED,
    HUB_MISSING,
    HubSecretClient,
    HubSecretIndex,
    HubFetchResult,
    HubSendResult,
    SecretOffer,
)
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.secret_store import KeyringSecretStore, SecretKeyStatus


class FakeHub(HubSecretClient):
    """A hub holding values in memory, with the base check the real one makes."""

    def __init__(self, locked: bool = False):
        self.values: dict[str, str] = {}
        self.held: dict[str, int] = {}
        self.locked = locked
        self.offered: list[SecretOffer] = []
        self.requested: list[list[str]] = []

    def index(self) -> HubSecretIndex:
        return HubSecretIndex(
            generations=dict(self.held),
            available=not self.locked,
            locked=self.locked,
        )

    def send(self, entries: list[SecretOffer]) -> list[HubSendResult]:
        self.offered.extend(entries)
        results = []
        for offer in entries:
            if self.locked:
                results.append(HubSendResult(key=offer.key, status=HUB_LOCKED))
                continue
            current = self.held.get(offer.key)
            if current is not None and current != offer.candidate - 1:
                results.append(HubSendResult(key=offer.key, status=HUB_CONFLICT))
                continue
            self.values[offer.key] = offer.value
            self.held[offer.key] = offer.candidate
            results.append(
                HubSendResult(
                    key=offer.key, status="stored", generation=offer.candidate
                )
            )
        return results

    def fetch(self, keys: list[str]) -> list[HubFetchResult]:
        self.requested.append(list(keys))
        results = []
        for key in keys:
            held = self.held.get(key)
            if held is None:
                results.append(HubFetchResult(key=key, status=HUB_MISSING))
                continue
            results.append(
                HubFetchResult(
                    key=key, status="sent", generation=held, value=self.values[key]
                )
            )
        return results


@pytest.fixture
def store(fake_keyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    return KeyringSecretStore(tmp_path / ".guildbotics" / "config")


def _status(store: KeyringSecretStore, key: str) -> SecretKeyStatus:
    state = store.key_state(key)
    assert state is not None
    return state.status


def test_sending_puts_the_value_on_the_hub_before_publishing_the_generation(store):
    store.set("A_TOKEN", "ghp-first")
    hub = FakeHub()

    outcomes = SecretTransfer(store, hub).send(["A_TOKEN"])

    assert [(o.key, o.status, o.generation) for o in outcomes] == [
        ("A_TOKEN", "sent", 1)
    ]
    assert hub.values == {"A_TOKEN": "ghp-first"}
    assert store.shared_generation("A_TOKEN") == 1
    assert _status(store, "A_TOKEN") is SecretKeyStatus.READY


def test_a_key_this_machine_has_no_value_for_is_not_sent(store):
    store.set("A_TOKEN", "ghp-first")
    SecretTransfer(store, FakeHub()).send(["A_TOKEN"])
    other = _second_device(store)

    outcomes = SecretTransfer(other, FakeHub()).send(["A_TOKEN"])

    assert [(o.key, o.status) for o in outcomes] == [("A_TOKEN", "no_value")]


def test_a_hub_that_moved_between_the_read_and_the_send_refuses(store):
    """The window the hub's own check exists for.

    A send builds on the generation it read from the hub. If another machine
    stores one in between, the second sender is refused rather than
    overwriting it -- and nothing was written here, so the key is untouched."""
    store.set("A_TOKEN", "from-windows")
    hub = _RacingHub("A_TOKEN", "from-mac", 1)

    outcomes = SecretTransfer(store, hub).send(["A_TOKEN"])

    assert [(o.key, o.status) for o in outcomes] == [("A_TOKEN", "conflict")]
    assert hub.values["A_TOKEN"] == "from-mac"
    assert store.shared_generation("A_TOKEN") == 0
    assert _status(store, "A_TOKEN") is SecretKeyStatus.PENDING_SEND
    assert store.get("A_TOKEN") == "from-windows"


def test_an_interrupted_send_is_settled_by_sending_past_it(store):
    """The value reached the hub and the answer did not.

    Sending again builds on what the hub holds, so the key lands on a recorded
    generation whether or not the earlier attempt arrived."""
    store.set("A_TOKEN", "ghp-first")
    hub = FakeHub()
    _interrupted_send(hub, "A_TOKEN", "ghp-first", 1)

    outcomes = SecretTransfer(store, hub).send(["A_TOKEN"])

    assert [(o.key, o.status, o.generation) for o in outcomes] == [
        ("A_TOKEN", "sent", 2)
    ]
    assert store.shared_generation("A_TOKEN") == 2
    assert _status(store, "A_TOKEN") is SecretKeyStatus.READY


def test_a_value_entered_again_still_settles_the_interrupted_send(store):
    """Re-entering the value is what the neighbouring state tells the user to
    do, and it must not be the thing that makes a key unrecoverable."""
    store.set("A_TOKEN", "ghp-first")
    hub = FakeHub()
    _interrupted_send(hub, "A_TOKEN", "ghp-second", 1)
    store.set("A_TOKEN", "ghp-third")

    outcomes = SecretTransfer(store, hub).send(["A_TOKEN"])

    assert [(o.key, o.status, o.generation) for o in outcomes] == [
        ("A_TOKEN", "sent", 2)
    ]
    assert hub.values["A_TOKEN"] == "ghp-third"
    assert _status(store, "A_TOKEN") is SecretKeyStatus.READY


def test_another_machine_settles_a_send_it_knows_nothing_about(store):
    """The machine that was sending is gone; the workspace still recovers,
    because the state is the hub's generation rather than a note on that
    machine."""
    store.set("A_TOKEN", "ghp-first")
    hub = FakeHub()
    SecretTransfer(store, hub).send(["A_TOKEN"])
    other = _second_device(store)
    other.adopt_received("A_TOKEN", "ghp-first", 1)
    _interrupted_send(hub, "A_TOKEN", "ghp-never-recorded", 2)

    outcomes = SecretTransfer(other, hub).send(["A_TOKEN"])

    assert [(o.key, o.status, o.generation) for o in outcomes] == [
        ("A_TOKEN", "sent", 3)
    ]
    assert hub.values["A_TOKEN"] == "ghp-first"
    assert _status(other, "A_TOKEN") is SecretKeyStatus.READY


def test_sending_a_key_changed_on_two_machines_keeps_this_machines_value(store):
    """Resolving a conflict in favour of this machine is a send the hub takes,
    not one it refuses: the other value is superseded as a new generation the
    other machine is told about."""
    store.set("A_TOKEN", "from-mac")
    hub = FakeHub()
    SecretTransfer(store, hub).send(["A_TOKEN"])
    other = _second_device(store)
    other.adopt_received("A_TOKEN", "from-mac", 1)
    other.set("A_TOKEN", "from-windows")
    store.set("A_TOKEN", "from-mac-again")
    SecretTransfer(store, hub).send(["A_TOKEN"])
    assert _status(other, "A_TOKEN") is SecretKeyStatus.CONFLICT

    outcomes = SecretTransfer(other, hub).send(["A_TOKEN"])

    assert [(o.key, o.status, o.generation) for o in outcomes] == [
        ("A_TOKEN", "sent", 3)
    ]
    assert hub.values["A_TOKEN"] == "from-windows"
    assert _status(other, "A_TOKEN") is SecretKeyStatus.READY


def test_a_locked_store_here_is_not_reported_as_an_empty_one(store):
    """The way out of a locked keychain is to unlock it, so the report has to
    say that rather than "this machine has no value"."""
    store.set("A_TOKEN", "ghp-first")
    locked = KeyringSecretStore(
        store.location.parent,
        local_index=store.location.parent.parent / "local" / "secrets.json",
        keychain=_LockedKeychain(),
    )

    outcomes = SecretTransfer(locked, FakeHub()).send(["A_TOKEN"])

    assert [(o.key, o.status) for o in outcomes] == [("A_TOKEN", "locked")]


def test_a_key_this_workspace_does_not_know_is_reported(store):
    """Otherwise a mistyped key name is a silent success."""
    outcomes = SecretTransfer(store, FakeHub()).send(["NO_SUCH_TOKEN"])

    assert [(o.key, o.status) for o in outcomes] == [("NO_SUCH_TOKEN", "unknown")]


def test_a_locked_hub_stores_nothing_and_says_so(store):
    store.set("A_TOKEN", "ghp-first")

    outcomes = SecretTransfer(store, FakeHub(locked=True)).send(["A_TOKEN"])

    assert [(o.key, o.status) for o in outcomes] == [("A_TOKEN", "locked")]
    assert store.shared_generation("A_TOKEN") == 0
    assert _status(store, "A_TOKEN") is SecretKeyStatus.PENDING_SEND


def test_a_new_device_fetches_everything_it_is_short_of_in_one_exchange(store):
    """The whole of setting up a machine: nothing is retyped, and nothing that
    this device already holds is asked for."""
    store.set("A_TOKEN", "ghp-first")
    store.set("B_TOKEN", "xoxb-first")
    hub = FakeHub()
    SecretTransfer(store, hub).send(["A_TOKEN", "B_TOKEN"])
    other = _second_device(store)
    # This device already holds B_TOKEN at the shared generation.
    other.adopt_received("B_TOKEN", "xoxb-first", 1)

    outcomes = SecretTransfer(other, hub).fetch_missing()

    assert [(o.key, o.status, o.generation) for o in outcomes] == [
        ("A_TOKEN", "fetched", 1)
    ]
    assert hub.requested == [["A_TOKEN"]]
    assert other.get("A_TOKEN") == "ghp-first"
    assert _status(other, "A_TOKEN") is SecretKeyStatus.READY


def test_fetching_replaces_a_superseded_value(store):
    store.set("A_TOKEN", "ghp-first")
    hub = FakeHub()
    SecretTransfer(store, hub).send(["A_TOKEN"])
    other = _second_device(store)
    other.adopt_received("A_TOKEN", "ghp-first", 1)
    # The first device rotates the token and shares it.
    store.set("A_TOKEN", "ghp-second")
    SecretTransfer(store, hub).send(["A_TOKEN"])
    assert _status(other, "A_TOKEN") is SecretKeyStatus.OUTDATED
    assert other.get("A_TOKEN") is None

    SecretTransfer(other, hub).fetch(["A_TOKEN"])

    assert other.get("A_TOKEN") == "ghp-second"
    assert _status(other, "A_TOKEN") is SecretKeyStatus.READY


def test_a_hub_generation_the_workspace_has_not_recorded_is_not_adopted(store):
    """The interrupted-send state is not resolved by spreading its value.

    A send that reached the hub and was cut off before the generation was
    published leaves the hub one ahead of the shared metadata. Taking that
    value here would clear the marker that says the two still disagree, and
    every machine would then report agreement over two different values."""
    store.set("A_TOKEN", "ghp-first")
    hub = FakeHub()
    SecretTransfer(store, hub).send(["A_TOKEN"])
    other = _second_device(store)
    other.adopt_received("A_TOKEN", "ghp-first", 1)
    _interrupted_send(hub, "A_TOKEN", "ghp-second", 2)

    outcomes = SecretTransfer(other, hub).fetch(["A_TOKEN"])

    assert [(o.key, o.status) for o in outcomes] == [("A_TOKEN", "generation_mismatch")]
    assert other.get("A_TOKEN") == "ghp-first"
    # Both machines can see the state, because the hub itself is the record.
    assert is_unconfirmed(other.key_state("A_TOKEN"), hub.held["A_TOKEN"])
    assert transfer_status(other.key_state("A_TOKEN"), hub.held["A_TOKEN"]) == (
        "unconfirmed"
    )


def test_the_bulk_fetch_leaves_out_the_states_it_cannot_settle(store):
    """Only what this machine is short of. An unconfirmed key is settled by
    finishing its send, and a conflicted one by a decision per key."""
    store.set("A_TOKEN", "ghp-first")
    store.set("B_TOKEN", "xoxb-first")
    store.set("C_TOKEN", "sk-first")
    hub = FakeHub()
    SecretTransfer(store, hub).send(["A_TOKEN", "B_TOKEN", "C_TOKEN"])
    other = _second_device(store)
    other.adopt_received("B_TOKEN", "xoxb-first", 1)
    other.adopt_received("C_TOKEN", "sk-first", 1)
    # B is changed here and elsewhere; C had a send reach the hub unrecorded.
    other.set("B_TOKEN", "xoxb-here")
    store.set("B_TOKEN", "xoxb-there")
    SecretTransfer(store, hub).send(["B_TOKEN"])
    _interrupted_send(hub, "C_TOKEN", "sk-elsewhere", 2)

    fetchable = bulk_fetch_keys(other.key_states(), hub.index())

    assert fetchable == ["A_TOKEN"]
    assert other.key_state("B_TOKEN").status is SecretKeyStatus.CONFLICT
    assert is_unconfirmed(other.key_state("C_TOKEN"), hub.held["C_TOKEN"])


def test_the_bulk_send_covers_everything_the_hub_would_gain(store):
    """A workspace only just connected has given the hub nothing, so "send
    everything" cannot mean only the values typed since."""
    store.set("A_TOKEN", "ghp-first")
    hub = FakeHub()
    SecretTransfer(store, hub).send(["A_TOKEN"])
    # A key held here at a recorded generation the hub knows nothing about, the
    # way a migrated or newly connected workspace's keys arrive.
    store.set("B_TOKEN", "xoxb-first")
    store.confirm_shared({"B_TOKEN": 1})
    assert _status(store, "B_TOKEN") is SecretKeyStatus.READY

    assert bulk_send_keys(store.key_states(), hub.index()) == ["B_TOKEN"]


def test_the_bulk_send_leaves_a_key_changed_on_two_machines_alone(store):
    """Sweeping it up would pick this machine's value without being asked."""
    store.set("A_TOKEN", "from-mac")
    hub = FakeHub()
    SecretTransfer(store, hub).send(["A_TOKEN"])
    other = _second_device(store)
    other.adopt_received("A_TOKEN", "from-mac", 1)
    other.set("A_TOKEN", "from-windows")
    store.set("A_TOKEN", "from-mac-again")
    SecretTransfer(store, hub).send(["A_TOKEN"])

    assert _status(other, "A_TOKEN") is SecretKeyStatus.CONFLICT
    assert bulk_send_keys(other.key_states(), hub.index()) == []


def test_a_key_the_hub_never_received_is_reported_as_missing(store):
    store.set("A_TOKEN", "ghp-first")
    other = _second_device(store)

    outcomes = SecretTransfer(other, FakeHub()).fetch(["A_TOKEN"])

    assert [(o.key, o.status) for o in outcomes] == [("A_TOKEN", "missing")]


class _LockedKeychain:
    """A machine whose OS secret store refuses everything until unlocked."""

    def validate_password(self, username: str, password: str) -> None:
        del username, password

    def get_password(self, service: str, username: str) -> str | None:
        raise SecretStoreError("the collection is locked")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise SecretStoreError("the collection is locked")

    def delete_password(self, service: str, username: str) -> None:
        raise SecretStoreError("the collection is locked")


class InMemoryKeychain:
    """One machine's OS secret store, separate from every other machine's."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def validate_password(self, username: str, password: str) -> None:
        del username, password

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


class _RacingHub(FakeHub):
    """A hub another machine writes to between the index read and the send."""

    def __init__(self, key: str, value: str, generation: int):
        super().__init__()
        self._pending = (key, value, generation)

    def index(self):
        answer = super().index()
        key, value, generation = self._pending
        self.held[key] = generation
        self.values[key] = value
        return answer


def _interrupted_send(hub: FakeHub, key: str, value: str, generation: int) -> None:
    """Leave the hub holding a generation the shared history does not name.

    What a send looks like when the value reached the hub and the answer did
    not: nothing was written on the sending machine, and the evidence is the
    hub's own generation running ahead of the recorded one."""
    hub.held[key] = generation
    hub.values[key] = value


def _second_device(store: KeyringSecretStore) -> KeyringSecretStore:
    """Another machine sharing the workspace: same shared index, nothing held.

    ``config/secrets.yml`` arrives there by synchronization, so both stores read
    the one file. What does not travel is the device-local record of what this
    machine holds, or the OS secret store itself, so the second store gets its
    own of each.
    """
    return KeyringSecretStore(
        store.location.parent,
        local_index=store.location.parent / "other-secrets.json",
        keychain=InMemoryKeychain(),
    )
