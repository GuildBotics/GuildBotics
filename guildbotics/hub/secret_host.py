"""The hub machine as the place every device fetches secret values from.

The hub keeps values in its own OS keychain and nowhere else. What it writes to
disk is one number per key -- the generation it holds -- which is what lets a
device tell "the hub has what I am looking for" from "the hub has something
older" without either side sending a value to find out.

Nothing here reads the workspace repository. The hub does not know what a
workspace contains, so the only agreement it can check is with itself: a device
says which generation it believes the hub holds, and a send is refused unless
that is the generation the hub actually holds. Two devices that changed the same
key from the same starting point cannot then both succeed, which is the whole of
the concurrency rule -- the second one is told, rather than overwriting the
first.
"""

from __future__ import annotations

import json
from pathlib import Path

from guildbotics.hub.host import HubError, HubNotHostedError, read_hub
from guildbotics.hub.host import workspace_repository_path as _repository_path
from guildbotics.utils.advisory_lock import held_lock
from guildbotics.utils.fileio import atomic_write_text
from guildbotics.utils.keychain import (
    InvalidSecretKeyError,
    Keychain,
    SecretStoreError,
    system_keychain,
)
from guildbotics.utils.secret_store import require_secret_key

#: The keychain service the hub stores one workspace's values under. It is
#: distinct from a device's own workspace service: the same machine can host a
#: hub and take part in the workspace, and the hub's copy is a distribution
#: point rather than that workspace's value.
KEYCHAIN_SERVICE_PREFIX = "GuildBotics-hub"
#: Where the hub records which generation it holds for each key.
GENERATIONS_FILENAME = "secret-generations.json"
GENERATIONS_SCHEMA_VERSION = 1


class HubSecretError(HubError):
    """Raised when the hub cannot complete a secret transfer."""


class HubSecretConflictError(HubSecretError):
    """Raised when a device sends from a generation the hub no longer holds.

    Another device shared a value for the same key in between. Nothing is
    overwritten: the sender is told, and the person decides which value wins.
    """


class HubSecretMissingError(HubSecretError):
    """Raised when the hub holds no value for a key that was asked for."""


#: Re-exported so a caller of this module has one place to catch what it
#: refuses. What counts as a key name is decided once, by the store that names
#: them, rather than a second time here.
__all__ = [
    "GENERATIONS_FILENAME",
    "HubSecretConflictError",
    "HubSecretError",
    "HubSecretMissingError",
    "InvalidSecretKeyError",
    "generations",
    "read_secret",
    "require_secret_key",
    "store_secret",
    "workspace_directory",
]


def workspace_directory(workspace_id: str) -> Path:
    """Return the hub directory of a workspace this hub actually holds.

    Raises:
        HubNotHostedError: When this machine hosts no hub.
        HubSecretError: When the workspace was never registered here. Creating
            it as a side effect of a secret transfer would let any reachable
            device name a directory into existence.
    """
    if read_hub() is None:
        raise HubNotHostedError(
            "This machine is not a hub. Make it one before transferring secrets."
        )
    repository = _repository_path(workspace_id)
    if not (repository / "HEAD").is_file():
        raise HubSecretError(f"This hub holds no workspace {workspace_id}.")
    return repository.parent


def generations(workspace_id: str) -> dict[str, int]:
    """Return the generation the hub holds for each key it has a value for.

    This is the whole of what a device needs to know before sending: the
    generation it publishes is the one after this. It is also how a send that
    was cut off before its generation was recorded becomes visible -- to every
    device, not only to the one that was sending.
    """
    return _read_generations(workspace_directory(workspace_id))


def store_secret(
    workspace_id: str,
    key: str,
    *,
    base_generation: int,
    candidate_generation: int,
    value: str,
    keychain: Keychain | None = None,
) -> int:
    """Take one value from a device and hold it as ``candidate_generation``.

    Args:
        workspace_id (str): The workspace the key belongs to.
        key (str): The logical key name.
        base_generation (int): The generation the sender believes the hub
            holds. ``0`` means "the hub holds nothing for this key yet".
        candidate_generation (int): The generation being published.
        value (str): The secret value. It is written to the keychain and to
            nothing else, and never appears in a return value or an error.
        keychain (Keychain | None): The store to use, for tests.

    Returns:
        int: The generation the hub now holds.

    Raises:
        HubSecretConflictError: When the hub holds a different generation.
        HubSecretError: When the generations are not a step forward, or the
            hub's keychain refused the value.
    """
    require_secret_key(key)
    directory = workspace_directory(workspace_id)
    # The generation space starts at 0 ("the hub holds nothing yet"), so this
    # boundary is where a negative number is refused once for every caller:
    # the wire, the local client, and the hub's own command all pass through
    # here, and the step rule below then guarantees the candidate is >= 1.
    if base_generation < 0:
        raise HubSecretError(
            f"{key} was sent from generation {base_generation}, which no hub "
            "could hold."
        )
    if candidate_generation != base_generation + 1:
        raise HubSecretError(
            f"{key} was sent as generation {candidate_generation} from "
            f"{base_generation}, which is not the next one."
        )
    with held_lock(directory / "secrets.lock"):
        held = _read_generations(directory)
        current = held.get(key)
        # Only a hub that has moved *ahead* of what the sender built on has
        # something to lose: another device stored a value in between, and that
        # value is the one every device is about to be told to fetch.
        #
        # A hub that is behind -- one that holds nothing, or that was restored
        # from an older copy -- has nothing anyone can obtain, because a device
        # takes only the generation the shared history names. Refusing those
        # would leave the workspace with no way to put a value back.
        if current is not None and current > base_generation:
            raise HubSecretConflictError(
                f"This hub holds generation {current} of {key}, not "
                f"{base_generation}. Another device shared it in between."
            )
        # The generation is recorded before the value, so an interruption
        # between the two leaves this hub claiming a generation it cannot serve
        # rather than serving a value under an older generation's name. The
        # first is the state the whole design already handles -- no device
        # fetches it, and the next send moves past it. The second would be
        # fetched by every device as though it were the value they agreed on.
        held[key] = candidate_generation
        _write_generations(directory, held)
        _keychain(keychain).set_password(_service(workspace_id), key, value)
    return candidate_generation


def read_secret(
    workspace_id: str, key: str, *, keychain: Keychain | None = None
) -> tuple[str, int]:
    """Return one value and the generation the hub holds it as.

    The two reads happen under the same lock :func:`store_secret` writes
    under. A store in between them would otherwise hand back the new value
    labelled with the old generation, and a device whose shared metadata still
    names the old one would adopt it -- two values under one generation, which
    is the exact state the generations exist to rule out.

    Raises:
        HubSecretMissingError: When the hub has no value for the key.
        SecretStoreError: When the hub's keychain is locked or unreachable.
    """
    require_secret_key(key)
    directory = workspace_directory(workspace_id)
    with held_lock(directory / "secrets.lock"):
        held = _read_generations(directory).get(key)
        if held is None:
            raise HubSecretMissingError(f"This hub holds no value for {key}.")
        value = _keychain(keychain).get_password(_service(workspace_id), key)
    if value is None:
        raise HubSecretMissingError(
            f"This hub records generation {held} of {key} but its keychain no "
            "longer holds the value."
        )
    return value, held


def _service(workspace_id: str) -> str:
    return f"{KEYCHAIN_SERVICE_PREFIX}/{workspace_id}"


def _keychain(keychain: Keychain | None) -> Keychain:
    if keychain is not None:
        return keychain
    try:
        return system_keychain()
    except Exception as exc:  # pragma: no cover - depends on the host install
        raise SecretStoreError(str(exc)) from exc


def _generations_path(directory: Path) -> Path:
    return directory / GENERATIONS_FILENAME


def _read_generations(directory: Path) -> dict[str, int]:
    path = _generations_path(directory)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HubSecretError(f"{path} is not readable.") from exc
    keys = payload.get("keys") if isinstance(payload, dict) else None
    if not isinstance(keys, dict):
        return {}
    # A hub only ever holds published generations, which start at 1. An entry
    # saying anything else is a corrupt record, and a corrupt record claims
    # nothing -- the same answer a hub that lost the file gives, and the one
    # state the workspace can always send its way out of.
    return {
        str(key): generation
        for key, generation in keys.items()
        if isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation >= 1
    }


def _write_generations(directory: Path, held: dict[str, int]) -> None:
    payload = {
        "schema_version": GENERATIONS_SCHEMA_VERSION,
        "keys": {key: held[key] for key in sorted(held)},
    }
    atomic_write_text(
        _generations_path(directory),
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
