"""Authenticated binary Hub transfer endpoint, independent of the open workspace."""

from ipaddress import ip_address
from typing import cast

from fastapi import Request, Response
from fastapi.concurrency import run_in_threadpool

from guildbotics.hub import secret_service


async def transfer(request: Request, workspace_id: str, operation: str) -> Response:
    """Keep raw bodies out of validation errors and diagnostics."""
    if request.client is None or not _loopback(request.client.host):
        return Response(status_code=403)
    if operation not in {"list", "send", "receive"}:
        return Response(status_code=404)
    try:
        # Decode neither values nor user-supplied metadata in the API layer.
        body = await request.body()
        answer = await run_in_threadpool(
            secret_service.handle,
            cast(secret_service.Operation, operation),
            workspace_id,
            body,
            tuple(request.query_params.getlist("key")),
        )
    except Exception:
        # Even an unexpected backend failure must not send a raw value to the
        # generic exception reporter. The status carries no request/exception text.
        return Response(status_code=400)
    return Response(
        answer,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


def _loopback(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False
