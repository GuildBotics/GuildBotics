"""Run Hub Secret requests in the OS session that can access the keychain."""

from __future__ import annotations

import sys
from http import HTTPStatus

import httpx

from guildbotics.hub import secret_service
from guildbotics.hub.secret_host import HubSecretError
from guildbotics.utils.local_api import read_endpoint


def execute(
    operation: secret_service.Operation,
    workspace_id: str,
    payload: bytes = b"",
    keys: tuple[str, ...] = (),
) -> bytes:
    """Delegate every macOS request to Desktop; other OSes execute directly.

    Values stay in framed bytes in memory. Neither failed responses nor HTTP
    exceptions are quoted, and a submitted request is never retried.
    """
    if sys.platform != "darwin":
        return secret_service.handle(operation, workspace_id, payload, keys)
    endpoint = read_endpoint()
    if endpoint is not None:
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{endpoint.port}",
                headers={"X-GuildBotics-Session-Token": endpoint.token},
                trust_env=False,
                timeout=30.0,
            ) as client:
                health = client.get("/health", timeout=2.0)
                health.raise_for_status()
                if (
                    health.json().get("service_instance_id")
                    == endpoint.service_instance_id
                ):
                    response = client.post(
                        f"/hub/secrets/{workspace_id}/{operation}",
                        params=[("key", key) for key in keys],
                        content=payload,
                        headers={"Content-Type": "application/octet-stream"},
                    )
                    if response.status_code == HTTPStatus.BAD_REQUEST:
                        raise HubSecretError("The Hub refused the secret request.")
                    response.raise_for_status()
                    return response.content
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
    return secret_service.handle(
        operation, workspace_id, payload, keys, unavailable=True
    )
