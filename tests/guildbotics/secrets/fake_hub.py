"""An in-memory Hub Secret client shared by boundary tests."""

from guildbotics.secrets.hub_client import (
    HUB_CONFLICT,
    HUB_LOCKED,
    HUB_MISSING,
    HubFetchResult,
    HubSecretClient,
    HubSecretIndex,
    HubSendResult,
    SecretOffer,
)


class FakeHub(HubSecretClient):
    """Hold values in memory while enforcing the real Hub's generation check."""

    def __init__(self, locked: bool = False) -> None:
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
        results: list[HubSendResult] = []
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
        results: list[HubFetchResult] = []
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
