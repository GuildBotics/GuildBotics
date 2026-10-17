"""What the provider routes of the App API meet, set up in the test.

The web a provider's setup reaches is answered here instead: provider setup
code builds its own HTTP clients, so the stand-in is installed where every
client is made. Each ``httpx.AsyncClient`` created while it is in place
answers from ``routes``, keyed by host and path, and a request to anywhere
else fails the test.

The saved workspace is a real one, served by the real runtime, so that a
route reads the saved team and its members' secrets the way the Desktop does.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.utils.secret_store import identity_secret_store
from tests.guildbotics import members

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}

Answer = Callable[[httpx.Request], httpx.Response]


def route_web(
    monkeypatch: pytest.MonkeyPatch, routes: dict[str, Answer]
) -> list[httpx.Request]:
    """Answer the provider's requests from ``routes``; return them as made."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        route = f"{request.url.host}{request.url.path}"
        if route not in routes:
            raise AssertionError(f"unexpected request to {request.url}")
        return routes[route](request)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = kwargs.get("transport") or transport
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return requests


def answer(payload: object, status_code: int = 200) -> Answer:
    """A JSON answer."""
    return lambda request: httpx.Response(status_code, json=payload)


def form_of(request: httpx.Request) -> dict[str, str]:
    """The fields of a form-encoded request."""
    return dict(httpx.QueryParams(request.content.decode()))


@contextmanager
def saved_workspace(
    workspace: Path, services: dict[str, Any] | None = None
) -> Iterator[TestClient]:
    """A client of the real runtime on a workspace set up with ``services``.

    ``workspace`` is expected to be the working directory, with ``HOME``
    pointing inside the test's own directory.
    """
    with TestClient(
        create_app(session_token="secret", runtime=AppRuntime(EventBus())),
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/config/init",
            headers=AUTH_HEADERS,
            json={
                "config_dir": str(workspace / ".guildbotics/config"),
                "language": "en",
                "services": services or {},
            },
        )
        assert response.status_code == 200, response.text
        yield client


def write_member(
    workspace: Path, person_id: str, body: str = "", **secrets: str
) -> None:
    """A saved member of ``workspace``, described whole ending in ``body``,
    holding ``secrets`` by their member-relative names."""
    members.write_member(
        workspace / ".guildbotics/config",
        f"person_id: {person_id}\nname: {person_id.title()}\n{body}",
    )
    store = identity_secret_store()
    prefix = person_id.upper().replace("-", "_") + "_"
    for name, value in secrets.items():
        store.set(prefix + name, value)
