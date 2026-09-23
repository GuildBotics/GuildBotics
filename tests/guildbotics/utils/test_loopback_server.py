"""A loopback server logs no request, whatever the process's logging is."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from guildbotics.utils.loopback_server import LoopbackServer


async def _answer(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "upgrade",
    [{}, {"connection": "Upgrade", "upgrade": "websocket"}],
    ids=["http", "websocket"],
)
async def test_a_request_is_not_logged_even_where_uvicorn_logging_is_handled(
    monkeypatch: pytest.MonkeyPatch, upgrade: dict[str, str]
) -> None:
    """What passes through belongs to the owner: a query can carry a secret.
    Whether uvicorn's loggers have a handler is up to the rest of the process,
    and can change after the server starts."""
    records = _Records()
    server = await LoopbackServer.start(lambda port: _answer)
    try:
        for name in ("uvicorn.access", "uvicorn.error"):
            logger = logging.getLogger(name)
            monkeypatch.setattr(logger, "handlers", [records])
            monkeypatch.setattr(logger, "level", logging.INFO)
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://127.0.0.1:{server.port}/path?secret=QUERY-459",
                headers={
                    **upgrade,
                    "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
                    "sec-websocket-version": "13",
                },
            )
    finally:
        await server.stop()

    assert response.status_code == 204
    assert not [record for record in records.records if "QUERY-459" in record]
