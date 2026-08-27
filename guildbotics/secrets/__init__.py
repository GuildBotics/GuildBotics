"""Distributing secret values across the devices that share one workspace.

The shared history carries key names and generations; the values themselves
never enter it. What closes that gap is this package: the hub machine's own
secret store is the distribution point, and a device sends to it or fetches
from it only when the user says so.

It sits above :mod:`guildbotics.hub`, which is how a device reaches that
machine, and above :mod:`guildbotics.utils.secret_store`, which owns this
machine's values and the metadata the rest of the workspace synchronizes. It
knows nothing about the API layer or the Desktop.
"""

from __future__ import annotations

from guildbotics.secrets.hub_client import (
    HubSecretClient,
    HubSecretIndex,
    LocalHubSecretClient,
    RemoteHubSecretClient,
    SecretOffer,
    hub_secret_client,
)
from guildbotics.secrets.transfer import (
    CONFLICT,
    FETCHED,
    GENERATION_MISMATCH,
    HUB_BEHIND,
    LOCKED,
    MISSING,
    NO_VALUE,
    SENT,
    STORE_UNAVAILABLE,
    UNCONFIRMED,
    UNKNOWN,
    SecretPublishError,
    SecretTransfer,
    SecretTransferOutcome,
    bulk_fetch_keys,
    bulk_send_keys,
    can_fetch,
    can_send,
    is_hub_behind,
    is_unconfirmed,
    transfer_status,
)

__all__ = [
    "CONFLICT",
    "FETCHED",
    "GENERATION_MISMATCH",
    "HUB_BEHIND",
    "LOCKED",
    "MISSING",
    "NO_VALUE",
    "SENT",
    "STORE_UNAVAILABLE",
    "UNCONFIRMED",
    "UNKNOWN",
    "HubSecretClient",
    "HubSecretIndex",
    "LocalHubSecretClient",
    "RemoteHubSecretClient",
    "SecretOffer",
    "SecretPublishError",
    "SecretTransfer",
    "SecretTransferOutcome",
    "bulk_fetch_keys",
    "bulk_send_keys",
    "can_fetch",
    "can_send",
    "hub_secret_client",
    "is_hub_behind",
    "is_unconfirmed",
    "transfer_status",
]
