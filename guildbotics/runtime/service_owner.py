"""Shared service-owner preparation for Desktop and the CLI service."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal
from uuid import uuid4

from guildbotics.hub import HubUnreachableError, connection
from guildbotics.hub.relay_client import HubRelayClient, HubRelayClientError
from guildbotics.runtime.live_state import LiveState
from guildbotics.runtime.relay_runtime import RelayRuntime

ServiceOwnerErrorCode = Literal[
    "hub_unavailable", "service_owner_unavailable", "service_owner_conflict"
]


class ServiceOwnerError(RuntimeError):
    """A service cannot be prepared with the current Hub authority."""

    def __init__(
        self,
        code: ServiceOwnerErrorCode,
        message: str,
        *,
        owner_device_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.owner_device_id = owner_device_id


def create_relay_runtime(
    remote_url: str,
    workspace_id: str,
    device_id: str,
    *,
    on_live_state: Callable[[LiveState], None] | None = None,
    on_live_expired: Callable[[str, str, str], None] | None = None,
    on_head_updated: Callable[[], None] | None = None,
    on_relay_error: Callable[[str], None] | None = None,
) -> RelayRuntime:
    """Build the common relay component from a repository remote."""
    client: HubRelayClient | None = None
    try:
        location = connection.location_from_remote_url(remote_url, workspace_id)
        client = HubRelayClient(location, workspace_id, device_id, str(uuid4()))
        return RelayRuntime(
            client,
            on_live_state=on_live_state,
            on_live_expired=on_live_expired,
            on_head_updated=on_head_updated,
            on_relay_error=on_relay_error,
        )
    except (HubRelayClientError, HubUnreachableError, OSError, ValueError) as exc:
        if client is not None:
            client.close()
        raise ServiceOwnerError(
            "hub_unavailable", f"The Hub is unavailable: {exc}"
        ) from exc


def prepare_service_owner(
    runtime: RelayRuntime,
    device_id: str,
    *,
    start_runtime: bool = True,
) -> RelayRuntime:
    """Require this device to own the service, then optionally start relay."""
    try:
        owner = runtime.client.owner_get()
        if owner is None:
            owner = runtime.client.owner_claim().owner
    except (HubRelayClientError, OSError, RuntimeError, ValueError) as exc:
        raise ServiceOwnerError(
            "hub_unavailable", f"The Hub is unavailable: {exc}"
        ) from exc
    if owner is None:
        raise ServiceOwnerError(
            "service_owner_unavailable", "The Hub did not return a service owner."
        )
    if owner.owner_device_id != device_id:
        raise ServiceOwnerError(
            "service_owner_conflict",
            "This device is not the service owner. Transfer ownership explicitly first.",
            owner_device_id=owner.owner_device_id,
        )
    if start_runtime:
        runtime.start()
    return runtime
