"""Moving secret values between this device and the hub, when asked to.

Nothing here runs on its own. A value leaves this machine because the user
pressed "send", and arrives because the user pressed "fetch": the synchronized
metadata says which keys are in which state, and the person decides what to do
about it. That is the whole reason the hub holds values at all -- the shared
history carries key names and generations, and never a value.

The order of the two writes in a send is the part worth reading twice. The hub
takes the value first, and only then does this device publish the generation
that names it. Publishing first would tell every other machine to fetch
something that is not there.

The two stores cannot be written atomically, and what falls between them is not
hidden -- but neither is it written down on the machine that was sending. It is
already legible from the two records that matter: the hub holding a generation
higher than the one the shared history names *is* a send that was cut off, and
every device can see it. A note kept on the sending machine could say the same,
and would not survive the value being entered again there, or that machine
being lost; the comparison survives both.

Which transfer applies to which key is decided by the functions at the top of
this module and nowhere else. The CLI, the API, and the screen all read the
same answer, so a button cannot offer a transfer the transfer itself refuses.
"""

from __future__ import annotations

from dataclasses import dataclass

from guildbotics.secrets.hub_client import (
    HUB_CONFLICT,
    HUB_MISSING,
    HubSecretClient,
    HubSecretIndex,
    SecretOffer,
)
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.secret_store import (
    UNSHARED_GENERATION,
    KeyringSecretStore,
    SecretKeyState,
    SecretKeyStatus,
    is_locked_error,
)

#: What a key's standing is called when the hub holds a generation the shared
#: history does not name. It is not one of the device's own states: only the
#: two records together say it.
UNCONFIRMED = "unconfirmed"

#: The value reached the hub and the shared generation now names it.
SENT = "sent"
#: The value arrived and this device now holds the hub's generation.
FETCHED = "fetched"
#: This device holds no value to send for that key.
NO_VALUE = "no_value"
#: The workspace does not know that key at all.
UNKNOWN = "unknown"
#: The hub holds a generation the shared metadata does not name. Taking it
#: would hide the very state that says the two have to be reconciled.
GENERATION_MISMATCH = "generation_mismatch"
#: The hub's own words for why it could not help, reused so that a hub on this
#: machine and a hub over SSH are reported identically.
CONFLICT = HUB_CONFLICT
MISSING = HUB_MISSING
#: This machine's secret store refused, which the hub never saw.
LOCKED = "locked"
STORE_UNAVAILABLE = "store_unavailable"


def is_unconfirmed(state: SecretKeyState, hub_generation: int | None) -> bool:
    """Whether the hub holds a generation nobody recorded.

    That is a send which reached the hub and was cut off before its generation
    was published. No device can fetch the value -- fetching takes only the
    recorded generation -- so it is inert until some device sends past it, and
    a send does exactly that by building on what the hub holds.
    """
    return hub_generation is not None and hub_generation > state.shared_generation


def transfer_status(state: SecretKeyState, hub_generation: int | None) -> str:
    """What one key's standing is, given both records.

    The device knows most of it on its own; the one thing it cannot know
    without the hub is that an earlier send landed without being recorded.
    """
    if is_unconfirmed(state, hub_generation):
        return UNCONFIRMED
    return str(state.status)


def can_send(state: SecretKeyState) -> bool:
    """Whether this device has a value for ``state`` worth offering the hub.

    A key that is already in step can be sent as well: that is how a hub which
    never held it -- a rebuilt one, or one this workspace has only just
    connected to -- is given a copy. What cannot be sent is a value this
    machine does not have, or one another device has already replaced.
    """
    return state.status not in {SecretKeyStatus.MISSING, SecretKeyStatus.OUTDATED}


def can_fetch(state: SecretKeyState, hub_generation: int | None) -> bool:
    """Whether the hub can hand this device a value for ``state``.

    Only the generation the workspace agreed on is ever taken. A hub holding
    something the shared metadata does not name is the interrupted-send state,
    and the way out of it is to finish that send, not to spread its value.

    A key changed on two machines is offered as well, because taking the other
    machine's value is one of the two ways out of that -- but only from the
    row, never from the bulk action, since it drops what was typed here.
    """
    if hub_generation != state.shared_generation:
        return False
    return state.status in {
        SecretKeyStatus.MISSING,
        SecretKeyStatus.OUTDATED,
        SecretKeyStatus.CONFLICT,
    }


def bulk_send_keys(
    states: list[SecretKeyState], hub: HubSecretIndex | None
) -> list[str]:
    """The keys "send everything" hands the hub.

    Everything the hub would gain: values entered here that it has not been
    given, handovers it may not have finished taking, and -- on a workspace
    that has only just been connected, or a hub that was rebuilt -- every value
    it holds nothing for.

    A key changed on two machines is left out, the same way the bulk fetch
    leaves it out. Sending it makes this machine's value the one every device
    gets, and choosing between two edits is a decision to take one key at a
    time rather than to sweep up.

    A hub that did not answer names nothing: what it would gain is not
    something to guess at, and offering to send everything to a machine that
    cannot be reached is an action that can only fail.
    """
    if hub is None:
        return []
    held = hub.generations
    return [
        state.key
        for state in states
        if can_send(state)
        and state.status is not SecretKeyStatus.CONFLICT
        and (
            state.pending_send
            or is_unconfirmed(state, held.get(state.key))
            or held.get(state.key) != state.shared_generation
        )
    ]


def bulk_fetch_keys(
    states: list[SecretKeyState], hub: HubSecretIndex | None
) -> list[str]:
    """The keys "fetch everything" asks the hub for.

    Only what this machine is short of. A key changed on two machines is left
    out on purpose: fetching it discards what was typed here, which is a
    decision to take one key at a time and not one to sweep up.
    """
    if hub is None:
        return []
    held = hub.generations
    return [
        state.key
        for state in states
        if state.status in {SecretKeyStatus.MISSING, SecretKeyStatus.OUTDATED}
        and can_fetch(state, held.get(state.key))
    ]


@dataclass(frozen=True)
class SecretTransferOutcome:
    """What happened to one key in one transfer.

    Attributes:
        key (str): The logical key name.
        status (str): One of this module's status constants.
        generation (int | None): The generation now agreed for the key.
    """

    key: str
    status: str
    generation: int | None = None


class SecretTransfer:
    """One workspace's secret transfers, against one hub."""

    def __init__(self, store: KeyringSecretStore, client: HubSecretClient):
        self._store = store
        self._client = client

    def hub_index(self) -> HubSecretIndex:
        """Return what the hub holds, so a state can name both sides."""
        return self._client.index()

    def send(self, keys: list[str]) -> list[SecretTransferOutcome]:
        """Hand the hub this device's value for each key.

        What the hub already holds is read first, because that is what a send
        builds on: the generation published is the one after the hub's, not the
        one after this device's. That single rule is what makes every state
        reachable from the outside -- see :meth:`_offer`.
        """
        known = self._known()
        held = self.hub_index().generations if keys else {}
        offers: list[SecretOffer] = []
        outcomes: list[SecretTransferOutcome] = []
        for key in keys:
            state = known.get(key)
            if state is None:
                outcomes.append(SecretTransferOutcome(key, UNKNOWN))
                continue
            offer, refusal = self._offer(state, held.get(key))
            if offer is None:
                outcomes.append(SecretTransferOutcome(key, refusal or NO_VALUE))
                continue
            offers.append(offer)
        confirmed: dict[str, int] = {}
        # Nothing to hand over is not a reason to reach the hub: on another
        # machine that would be an SSH connection carrying nothing.
        for result in self._client.send(offers) if offers else []:
            if result.status == "stored" and result.generation is not None:
                confirmed[result.key] = result.generation
                outcomes.append(
                    SecretTransferOutcome(result.key, SENT, result.generation)
                )
                continue
            # Every other status is an answer, which means the hub reached its
            # own check and stored nothing. Nothing local was written either
            # way, so a refused send leaves the key exactly as it was.
            outcomes.append(SecretTransferOutcome(result.key, result.status))
        self._store.confirm_shared(confirmed)
        return _ordered(outcomes, keys)

    def fetch(self, keys: list[str]) -> list[SecretTransferOutcome]:
        """Take the hub's value for each key into this device's own store.

        Only the generation the workspace agreed on is adopted. The hub holding
        a newer one means a send from some machine never finished being
        recorded, and storing that value here would clear the very marker that
        says so -- leaving every machine reporting agreement over two different
        values.
        """
        known = self._known()
        outcomes: list[SecretTransferOutcome] = []
        for result in self._client.fetch(list(keys)) if keys else []:
            if result.status != "sent" or result.value is None:
                outcomes.append(SecretTransferOutcome(result.key, result.status))
                continue
            state = known.get(result.key)
            shared = (
                state.shared_generation if state is not None else UNSHARED_GENERATION
            )
            if result.generation != shared:
                outcomes.append(
                    SecretTransferOutcome(
                        result.key, GENERATION_MISMATCH, result.generation
                    )
                )
                continue
            try:
                self._store.adopt_received(result.key, result.value, shared)
            except SecretStoreError as exc:
                outcomes.append(SecretTransferOutcome(result.key, _local_status(exc)))
                continue
            outcomes.append(SecretTransferOutcome(result.key, FETCHED, shared))
        return _ordered(outcomes, keys)

    def fetch_missing(self) -> list[SecretTransferOutcome]:
        """Fetch every key this device has no value for or an older value for.

        This is the one action a device added to an existing workspace needs.
        It asks for everything it is short of in a single exchange and puts each
        value straight into the OS secret store, so nothing is retyped -- and it
        asks for nothing this machine already holds.
        """
        return self.fetch(bulk_fetch_keys(self._store.key_states(), self.hub_index()))

    def send_pending(self) -> list[SecretTransferOutcome]:
        """Send every value the hub would gain from this device."""
        return self.send(bulk_send_keys(self._store.key_states(), self.hub_index()))

    def _offer(
        self, state: SecretKeyState, hub_generation: int | None
    ) -> tuple[SecretOffer | None, str | None]:
        """Build the offer for one key, or say why there is nothing to send.

        The generation offered is the one after what the *hub* holds, never
        after what this device holds. One rule, and it is what makes every
        state reachable from the outside:

        * A hub holding a generation the workspace never recorded is an earlier
          send that was cut off before its generation was published. No device
          fetches that value -- fetching takes only the recorded generation --
          so superseding it spreads nothing and withholds nothing, and it is
          the way past that state for whoever sends next, including a machine
          that knows nothing of the send that was interrupted.
        * A key changed on two machines sends from the recorded generation, so
          "send mine" is a value the hub accepts rather than one it refuses,
          and the other machine's value is superseded visibly, as a generation
          it is told about, rather than quietly.
        * A hub that holds nothing for the key -- a rebuilt one, or a workspace
          only just connected -- takes whatever generation the workspace had
          reached.

        The hub checks the same number again before storing, which is what
        settles two machines that read it at the same moment: the second one is
        refused rather than overwriting the first.
        """
        if not can_send(state):
            return None, NO_VALUE
        try:
            value = self._store.get(state.key)
        except SecretStoreError as exc:
            # The store refused, which the hub never saw. Saying "no value"
            # here would describe a locked keychain as an empty one.
            return None, _local_status(exc)
        if value is None:
            return None, NO_VALUE
        base = max(state.shared_generation, hub_generation or UNSHARED_GENERATION)
        return SecretOffer(key=state.key, candidate=base + 1, value=value), None

    def _known(self) -> dict[str, SecretKeyState]:
        return {state.key: state for state in self._store.key_states()}


def _ordered(
    outcomes: list[SecretTransferOutcome], keys: list[str]
) -> list[SecretTransferOutcome]:
    """Report outcomes in the order the caller asked for the keys."""
    found = {outcome.key: outcome for outcome in outcomes}
    named = set(keys)
    return [found[key] for key in keys if key in found] + [
        outcome for outcome in outcomes if outcome.key not in named
    ]


def _local_status(exc: SecretStoreError) -> str:
    return LOCKED if is_locked_error(exc) else STORE_UNAVAILABLE
