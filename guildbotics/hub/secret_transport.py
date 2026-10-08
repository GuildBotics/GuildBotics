"""Run Hub Secret requests in the OS session that can access the keychain."""

from __future__ import annotations

import sys
from http import HTTPStatus

import httpx

from guildbotics.hub import host, secret_service
from guildbotics.hub.secret_host import HubSecretError
from guildbotics.utils.local_api import connect_local_api

DELEGATES_TO_DESKTOP = sys.platform == "darwin"


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
    host.require_workspace_id(workspace_id)
    if not DELEGATES_TO_DESKTOP:
        return secret_service.handle(operation, workspace_id, payload, keys)
    try:
        with connect_local_api(timeout=30.0) as client:
            if client is not None:
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
    except httpx.HTTPError:
        pass
    return secret_service.handle(
        operation, workspace_id, payload, keys, unavailable=True
    )
