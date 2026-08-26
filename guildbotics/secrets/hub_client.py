"""Talking to a hub about secret values, wherever the hub happens to be.

Two clients answer the same three questions -- what do you hold, take this,
give me that -- so the part above them that decides *which* keys to move never
learns whether the hub is another machine or this one. Only the remote client
frames anything: over SSH the exchange has to survive a pipe, while a hub on
this machine is simply called.
"""

from __future__ import annotations

import io
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from guildbotics.hub import secret_host, secret_stream
from guildbotics.hub.connection import HubEndpoint, HubLocation, run_hub_stream
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.secret_store import (
    is_locked_error,
    is_secret_key,
    keyring_status,
)

#: What a hub can say about one key it was asked for or sent, other than
#: success. They are the values the hub's own commands report, so a remote and
#: a local hub answer with the same words.
HUB_MISSING = "missing"
HUB_CONFLICT = "conflict"
HUB_LOCKED = "locked"
HUB_UNAVAILABLE = "store_unavailable"
HUB_INVALID = "invalid"


@dataclass(frozen=True)
class SecretOffer:
    """One value a device hands to the hub.

    Attributes:
        key (str): The logical key name.
        candidate (int): The generation being published. Its predecessor is the
            generation the sender read from the hub, so the hub can refuse a
            value built on something it has moved past since.
        value (str): The secret value.
    """

    key: str
    candidate: int
    value: str


@dataclass(frozen=True)
class HubSecretIndex:
    """What a hub holds, without any value.

    Attributes:
        generations (dict[str, int]): The generation held per key.
        available (bool): Whether the hub's own secret store answered.
        locked (bool): Whether it answered that it is locked. A locked hub can
            still report which generations it holds, so the two are separate.
    """

    generations: dict[str, int] = field(default_factory=dict)
    available: bool = True
    locked: bool = False


@dataclass(frozen=True)
class HubSendResult:
    """The hub's answer to one value it was offered."""

    key: str
    status: str
    generation: int | None = None


@dataclass(frozen=True)
class HubFetchResult:
    """The hub's answer to one value it was asked for."""

    key: str
    status: str
    generation: int | None = None
    value: str | None = None


class HubSecretClient(ABC):
    """The three things a device asks a hub about secret values."""

    @abstractmethod
    def index(self) -> HubSecretIndex:
        """Return the generations the hub holds, and its store's state."""

    @abstractmethod
    def send(self, entries: list[SecretOffer]) -> list[HubSendResult]:
        """Offer values for the hub to hold, each naming its own handover."""

    @abstractmethod
    def fetch(self, keys: list[str]) -> list[HubFetchResult]:
        """Ask for the current value of each key."""


class LocalHubSecretClient(HubSecretClient):
    """A hub hosted on this same machine, called rather than connected to."""

    def __init__(self, workspace_id: str):
        self._workspace_id = workspace_id

    def index(self) -> HubSecretIndex:
        status = keyring_status()
        return HubSecretIndex(
            generations=secret_host.generations(self._workspace_id),
            available=bool(status["available"]),
            locked=bool(status["locked"]),
        )

    def send(self, entries: list[SecretOffer]) -> list[HubSendResult]:
        results: list[HubSendResult] = []
        for offer in entries:
            if not is_secret_key(offer.key):
                results.append(HubSendResult(key=offer.key, status=HUB_INVALID))
                continue
            try:
                generation = secret_host.store_secret(
                    self._workspace_id,
                    offer.key,
                    base_generation=offer.candidate - 1,
                    candidate_generation=offer.candidate,
                    value=offer.value,
                )
            except secret_host.HubSecretConflictError:
                results.append(HubSendResult(key=offer.key, status=HUB_CONFLICT))
                continue
            except SecretStoreError as exc:
                results.append(HubSendResult(key=offer.key, status=_store_status(exc)))
                continue
            results.append(
                HubSendResult(key=offer.key, status="stored", generation=generation)
            )
        return results

    def fetch(self, keys: list[str]) -> list[HubFetchResult]:
        named, refused = _named_keys(keys)
        results = [HubFetchResult(key=key, status=HUB_INVALID) for key in refused]
        for key in named:
            try:
                value, generation = secret_host.read_secret(self._workspace_id, key)
            except secret_host.HubSecretMissingError:
                results.append(HubFetchResult(key=key, status=HUB_MISSING))
                continue
            except SecretStoreError as exc:
                results.append(HubFetchResult(key=key, status=_store_status(exc)))
                continue
            results.append(
                HubFetchResult(
                    key=key, status="sent", generation=generation, value=value
                )
            )
        return results


class RemoteHubSecretClient(HubSecretClient):
    """A hub on another machine, reached by running its own command over SSH."""

    def __init__(self, endpoint: HubEndpoint, workspace_id: str):
        self._endpoint = endpoint
        self._workspace_id = workspace_id

    def index(self) -> HubSecretIndex:
        output = self._run(["list", self._workspace_id])
        try:
            payload = json.loads(output.decode("utf-8"))
            store = payload.get("secret_store") or {}
            return HubSecretIndex(
                generations={
                    str(key): generation
                    for key, generation in (payload.get("keys") or {}).items()
                    if _held_generation(generation) is not None
                },
                available=bool(store.get("available", True)),
                locked=bool(store.get("locked", False)),
            )
        except (ValueError, AttributeError, TypeError) as exc:
            raise secret_host.HubSecretError(
                f"{self._endpoint.target} did not answer with its secret index."
            ) from exc

    def send(self, entries: list[SecretOffer]) -> list[HubSendResult]:
        offered = [offer for offer in entries if is_secret_key(offer.key)]
        refused = [
            HubSendResult(key=offer.key, status=HUB_INVALID)
            for offer in entries
            if not is_secret_key(offer.key)
        ]
        if not offered:
            return refused
        payload = io.BytesIO()
        secret_stream.write_entries(
            payload,
            [
                secret_stream.SecretEntry(
                    key=offer.key,
                    value=offer.value.encode("utf-8"),
                    header={
                        "base_generation": offer.candidate - 1,
                        "candidate_generation": offer.candidate,
                    },
                )
                for offer in offered
            ],
        )
        output = self._run(["receive", self._workspace_id], payload.getvalue())
        try:
            results = json.loads(output.decode("utf-8"))["results"]
            return refused + [
                HubSendResult(
                    key=str(result["key"]),
                    status=str(result["status"]),
                    generation=result.get("generation"),
                )
                for result in results
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise secret_host.HubSecretError(
                f"{self._endpoint.target} did not answer the send with a result."
            ) from exc

    def fetch(self, keys: list[str]) -> list[HubFetchResult]:
        named, refused = _named_keys(keys)
        results = [HubFetchResult(key=key, status=HUB_INVALID) for key in refused]
        if not named:
            return results
        arguments = ["send", self._workspace_id]
        for key in named:
            arguments += ["--key", key]
        output = self._run(arguments)
        try:
            entries = list(secret_stream.read_entries(output))
        except secret_stream.SecretStreamError as exc:
            raise secret_host.HubSecretError(
                f"{self._endpoint.target} did not answer with framed values."
            ) from exc
        return results + [_fetched(entry) for entry in entries]

    def _run(self, arguments: list[str], payload: bytes = b"") -> bytes:
        return run_hub_stream(self._endpoint, ["secret", *arguments], payload)


def hub_secret_client(location: HubLocation, workspace_id: str) -> HubSecretClient:
    """Return the client for wherever this workspace's hub lives."""
    if location.endpoint is None:
        return LocalHubSecretClient(workspace_id)
    return RemoteHubSecretClient(location.endpoint, workspace_id)


def _named_keys(keys: list[str]) -> tuple[list[str], list[str]]:
    """Split keys into those that may be named on a command line, and the rest.

    A key reaches the hub as an argument to its own command, and OpenSSH hands
    that command line to a shell on the far side. What the hub would refuse on
    arrival is therefore refused before it is written, because by then a name
    carrying whitespace or a metacharacter has already been read as something
    other than a key.
    """
    named = [key for key in keys if is_secret_key(key)]
    return named, [key for key in keys if not is_secret_key(key)]


def _held_generation(value: object) -> int | None:
    """Read a generation a hub can actually hold: a positive int, or nothing.

    Published generations start at 1; ``0`` means "never shared" and lives
    only in a device's own records. Anything else arriving over the wire is a
    corrupt or hand-made answer, and adopting it would put a number into the
    workspace's state space that no legitimate send can produce.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


def _fetched(entry: secret_stream.SecretEntry) -> HubFetchResult:
    error = entry.header.get("error")
    if isinstance(error, str) and error:
        return HubFetchResult(key=entry.key, status=error)
    generation = _held_generation(entry.header.get("generation"))
    if generation is None:
        return HubFetchResult(key=entry.key, status=HUB_INVALID)
    try:
        value = entry.value.decode("utf-8")
    except UnicodeDecodeError:
        return HubFetchResult(key=entry.key, status=HUB_INVALID)
    return HubFetchResult(
        key=entry.key, status="sent", generation=generation, value=value
    )


def _store_status(exc: SecretStoreError) -> str:
    return HUB_LOCKED if is_locked_error(exc) else HUB_UNAVAILABLE
