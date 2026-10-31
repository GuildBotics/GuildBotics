"""Clients of the API as its owner: the token the server keeps in its owner
file, or a browser's session entered through an entry link."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from guildbotics.app_api.auth import CSRF_HEADER
from guildbotics.utils.local_api import API_PREFIX, base_url, owner_token_path

ORIGIN = base_url(8765)
#: ``TestClient.websocket_connect`` ignores the client's base URL.
EVENTS_URL = f"ws://127.0.0.1:8765{API_PREFIX}/events"


def owner_token(app: FastAPI) -> str:
    return owner_token_path(app.state.auth.port).read_text("utf-8").strip()


def owner_client(app: FastAPI, **kwargs: Any) -> TestClient:
    """A client of ``/api`` carrying the owner token, as the CLI does."""
    headers = {
        "Authorization": f"Bearer {owner_token(app)}",
        **kwargs.pop("headers", {}),
    }
    return TestClient(
        app,
        base_url=base_url(app.state.auth.port) + API_PREFIX,
        headers=headers,
        **kwargs,
    )


def browser_client(app: FastAPI, **kwargs: Any) -> TestClient:
    """A client of ``/api`` with a session entered through an entry link, its
    CSRF value on every request, from the server's own origin."""
    client = TestClient(
        app, base_url=base_url(app.state.auth.port) + API_PREFIX, **kwargs
    )
    link = owner_client(app).post("/session/links").json()["url"]
    session = client.post("/session", json={"code": link.split("#", 1)[1]})
    client.headers[CSRF_HEADER] = session.json()["csrf"]
    client.headers["Origin"] = base_url(app.state.auth.port)
    return client
