"""Every route that changes config writes under the workspace's shared lock.

The rule this file exists for is not "these particular routes take the lock"
but "no route that writes config can be added without taking it". A writer
outside the lock is invisible in review -- it looks like an ordinary save -- and
what it costs shows up on another machine, as a change that vanished without
being recorded as a conflict. So the routes are enumerated here against the
application's own routing table: a new one has to be classified before this
passes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

import pytest
from fastapi.testclient import TestClient

import guildbotics.app_api.avatar as avatar_module
import guildbotics.workspace.config_repository as config_repository_module
from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.workspace.shared_write_lock import shared_write_lock

HTTP_OK = 200

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}

#: Routes under ``/config`` that change nothing on disk, and why. Listing them
#: is what lets the coverage check below be exhaustive.
READ_ONLY_ROUTES = {
    ("POST", "/config/project/status-options"): "reads a GitHub project's lanes",
    ("POST", "/config/project/agent-field"): "reads a GitHub project's field",
    ("POST", "/config/project/agent-field/ensure"): "creates a field on GitHub",
    ("POST", "/config/members/resolve"): "resolves an identity, writes nothing",
    ("POST", "/config/members/github-app/registrations"): "starts an OAuth flow",
    ("POST", "/config/members/slack-app/registrations"): "starts an OAuth flow",
    ("POST", "/config/members/slack-app/verify"): "calls Slack",
}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def client(workspace: Path) -> TestClient:
    return TestClient(
        create_app(session_token="secret", runtime=AppRuntime(EventBus()))
    )


@pytest.fixture
def locked(monkeypatch: pytest.MonkeyPatch) -> list[Path | None]:
    """Record every acquisition of the shared-write lock, still taking it."""
    taken: list[Path | None] = []

    @contextmanager
    def recording(
        workspace_root: Path | None = None, **kwargs: Any
    ) -> Iterator[IO[str]]:
        taken.append(workspace_root)
        with shared_write_lock(workspace_root, **kwargs) as handle:
            yield handle

    monkeypatch.setattr(config_repository_module, "shared_write_lock", recording)
    return taken


def _config_dir(workspace: Path) -> Path:
    return workspace / ".guildbotics/config"


def _init(client: TestClient, workspace: Path) -> Path:
    config_dir = _config_dir(workspace)
    response = client.post(
        "/config/init",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(config_dir),
            "language": "en",
            "description": "Temp automation workspace",
            "llm_api_type": "openai",
            "cli_agent": "codex",
            "provider_api_keys": {"openai": "test-openai-key"},
        },
    )
    assert response.status_code == HTTP_OK
    return config_dir


def _add_member(client: TestClient, config_dir: Path) -> None:
    response = client.post(
        "/config/members",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(config_dir),
            "person_type": "",
            "person_id": "alice",
            "person_name": "Alice",
            "is_active": True,
            # The avatar imports need something to look the member up by.
            "github_username": "alice-gh",
            "git_email": "",
            "roles": ["architect"],
            "speaking_style": "concise",
        },
    )
    assert response.status_code == HTTP_OK


#: How to exercise each config-writing route, keyed the way FastAPI names it.
MUTATIONS: dict[tuple[str, str], Callable[[TestClient, Path], Any]] = {
    ("POST", "/config/init"): lambda client, workspace: _init(client, workspace),
    ("PUT", "/config/project"): lambda client, workspace: client.put(
        "/config/project",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(_config_dir(workspace)),
            "language": "en",
            "description": "Renamed",
            "llm_api_type": "openai",
            "cli_agent": "codex",
            "github_enabled": False,
        },
    ),
    ("PUT", "/config/project/default-person"): lambda client, workspace: client.put(
        "/config/project/default-person",
        headers=AUTH_HEADERS,
        json={"person_id": "alice"},
    ),
    ("POST", "/config/members"): lambda client, workspace: _add_member(
        client, _config_dir(workspace)
    ),
    ("PUT", "/config/members/{person_id}"): lambda client, workspace: client.put(
        "/config/members/alice",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(_config_dir(workspace)),
            "original_person_id": "alice",
            "person_type": "",
            "person_id": "alice",
            "person_name": "Renamed",
            "is_active": True,
            "github_username": "",
            "git_email": "",
            "roles": ["architect"],
            "speaking_style": "concise",
        },
    ),
    ("DELETE", "/config/members/{person_id}"): lambda client, workspace: client.request(
        "DELETE",
        "/config/members/alice",
        headers=AUTH_HEADERS,
        json={"config_dir": str(_config_dir(workspace))},
    ),
    ("PUT", "/config/intelligences"): lambda client, workspace: client.put(
        "/config/intelligences",
        headers=AUTH_HEADERS,
        json={"config_dir": str(_config_dir(workspace)), "person_id": None},
    ),
    ("POST", "/config/members/{person_id}/avatar"): lambda client, workspace: (
        client.post(
            "/config/members/alice/avatar",
            headers=AUTH_HEADERS,
            files={"file": ("avatar.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
    ),
    ("POST", "/config/members/{person_id}/avatar/github"): (
        lambda client, workspace: client.post(
            "/config/members/alice/avatar/github", headers=AUTH_HEADERS
        )
    ),
    ("POST", "/config/members/{person_id}/avatar/slack"): (
        lambda client, workspace: client.post(
            "/config/members/alice/avatar/slack", headers=AUTH_HEADERS
        )
    ),
}


def test_the_routes_that_change_config_are_all_classified(client: TestClient) -> None:
    """A new config route has to be sorted into one list or the other.

    Without this, the next writer added is simply not covered, and the omission
    reads as an ordinary endpoint.
    """
    declared = set(MUTATIONS) | set(READ_ONLY_ROUTES)
    routing_table = {
        (method, route.path)
        for route in client.app.routes  # type: ignore[attr-defined]
        for method in getattr(route, "methods", set())
        if str(getattr(route, "path", "")).startswith("/config")
        and method in {"POST", "PUT", "DELETE"}
    }

    assert routing_table == declared


@pytest.mark.parametrize(
    "route", sorted(MUTATIONS), ids=lambda route: f"{route[0]} {route[1]}"
)
def test_a_config_change_is_written_under_the_shared_write_lock(
    client: TestClient,
    workspace: Path,
    locked: list[Path | None],
    monkeypatch: pytest.MonkeyPatch,
    route: tuple[str, str],
) -> None:
    """Synchronization checks the hub's content out over these same files.

    A writer that skips the lock can land in the middle of that, and the result
    is committed and pushed as if the user had made it.
    """
    monkeypatch.setattr(
        avatar_module, "get_github_avatar_url", _fake_url, raising=False
    )
    monkeypatch.setattr(avatar_module, "get_slack_avatar_url", _fake_url, raising=False)
    monkeypatch.setattr(avatar_module, "download_avatar", _fake_download, raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    config_dir = _init(client, workspace)
    if route != ("POST", "/config/members"):
        _add_member(client, config_dir)
    locked.clear()

    MUTATIONS[route](client, workspace)

    assert locked, f"{route[0]} {route[1]} wrote config without the lock"


async def _fake_url(*_args: object, **_kwargs: object) -> str:
    return "https://example.invalid/avatar.png"


async def _fake_download(_url: str) -> tuple[bytes, str]:
    return b"\x89PNG\r\n\x1a\n", ".png"


def _impatient(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    """Make ``module``'s lock give up at once instead of after 30 seconds."""

    @contextmanager
    def brief(workspace_root: Path | None = None, **_: Any) -> Iterator[IO[str]]:
        with shared_write_lock(workspace_root, timeout=0.05) as handle:
            yield handle

    monkeypatch.setattr(module, "shared_write_lock", brief)


def test_a_save_that_cannot_take_the_lock_is_a_503_rather_than_a_crash(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Waiting on the other writer is not a failure of the request.

    A lock timeout is a ``TimeoutError``, so the family it belongs to would
    otherwise be read as the environment breaking -- a 500 here, and the hub
    reported unreachable on the synchronization side.
    """
    config_dir = _init(client, workspace)
    _impatient(monkeypatch, config_repository_module)

    with shared_write_lock(workspace):
        response = client.put(
            "/config/project/default-person",
            headers=AUTH_HEADERS,
            json={"person_id": ""},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "config_busy"
    # Nothing was written, so the screen may simply try again.
    assert (config_dir / "team/project.yml").exists()
