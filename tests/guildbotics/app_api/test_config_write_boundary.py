"""Every route that changes config goes through the one comparing writer.

Exclusion is no longer the question here: the sync port takes the workspace's
shared-write lock for every shared write, so a route cannot land in the middle
of a synchronization cycle whatever it does. What a route still has to do for
itself is compare -- write through ``ConfigRepository.write``, so the revisions
the screen composed against are checked before anything is written and the new
ones come back in the answer. A route that skips that saves over another
screen's change and reports success.

So the population is every route that changes anything, taken from the
application's own routing table, and each one is classified here. It is
deliberately not "the routes under ``/config``": config lives under that prefix
but is not only written from it -- commands and transcript settings are config
files reached from elsewhere, and drawing the boundary around the URL is how
they came to be missed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import guildbotics.app_api.avatar as avatar_module
import guildbotics.workspace.config_repository as config_repository_module
from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.utils.shared_write_lock import shared_write_lock

HTTP_OK = 200
HTTP_SERVICE_UNAVAILABLE = 503

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}

#: Routes that change something other than the workspace's shared config, and
#: what they change instead. Naming them is what lets the check below be
#: exhaustive rather than a list of the routes someone happened to think of.
#: Nothing here is unprotected: shared writes still go through the port, which
#: serializes them. They simply have nobody to compare revisions with.
#:
#: The secret transfers are the one entry that does touch a config file. They
#: publish generations into ``config/secrets.yml`` through the secret store,
#: which re-reads that file inside the port's lock immediately before writing
#: it. There is no snapshot to compare against: no screen composes that file,
#: and the two other writers of it -- the setup service and the ``secrets``
#: CLI -- have never gone through the comparing writer either. What the guard
#: is for is a route that saves over a revision a screen was holding, and this
#: is not one.
ELSEWHERE = {
    ("POST", "/chat/receive-state/reset"): "state/, settled by first-committer-wins",
    ("POST", "/commands/author"): "proposes a change set, writes nothing",
    ("POST", "/commands/input-files"): "a temporary file outside the workspace",
    ("POST", "/commands/run"): "starts a run; its own writes go through the port",
    ("POST", "/config/members/resolve"): "resolves an identity, writes nothing",
    ("POST", "/config/members/github-app/registrations"): "starts an OAuth flow",
    ("POST", "/config/members/slack-app/registrations"): "starts an OAuth flow",
    ("POST", "/config/members/slack-app/verify"): "calls Slack",
    ("POST", "/config/project/agent-field"): "reads a GitHub project's field",
    ("POST", "/config/project/agent-field/ensure"): "creates a field on GitHub",
    ("POST", "/config/project/status-options"): "reads a GitHub project's lanes",
    ("POST", "/diagnostics/scenario"): "local/run diagnostics",
    ("POST", "/diagnostics/troubleshoot"): "local/run diagnostics",
    ("POST", "/hub"): "~/.guildbotics/hub, outside any workspace",
    ("POST", "/hub/inspect"): "reads a hub",
    ("POST", "/hub/ssh-key"): "~/.ssh",
    ("POST", "/hub/trust"): "~/.ssh/known_hosts",
    ("POST", "/scheduler/start"): "runtime lifecycle",
    ("POST", "/scheduler/stop"): "runtime lifecycle",
    ("POST", "/system-alerts/dismiss"): "local/run state",
    ("POST", "/verify"): "runs checks",
    ("POST", "/workspace"): "selects a workspace",
    ("POST", "/workspace/devices/self"): "state/, settled by first-committer-wins",
    (
        "POST",
        "/workspace/secrets/fetch",
    ): "OS secret stores; writes no config",
    (
        "POST",
        "/workspace/secrets/send",
    ): "one key generation, read-modify-written inside the port's lock",
    ("POST", "/workspace/sync/clone"): "a whole workspace, not one config file",
    ("POST", "/workspace/sync/enable"): "a whole workspace, not one config file",
    ("POST", "/workspace/sync/hub"): "a whole workspace, not one config file",
    ("POST", "/workspace/sync/preview"): "compares two histories, writes no config",
    (
        "POST",
        "/workspace/sync/rejections/{rejection_id}/discard",
    ): "deletes a local ref, writes no config",
    ("POST", "/workspace/sync/retry"): "a whole workspace, not one config file",
    ("POST", "/workspace/service-owner/transfer"): "Hub service-owner relay state",
    ("PUT", "/hotkeys"): "local/hotkeys.yml is device-specific by design",
    ("PUT", "/runtime/debug"): "a runtime flag",
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
def compared(monkeypatch: pytest.MonkeyPatch) -> list[Path | None]:
    """Record every pass through the comparing writer, still letting it run.

    ``ConfigRepository.write`` is the only thing that takes the lock in that
    module, so watching the lock is how a route is observed to have gone
    through it rather than writing the file some other way.
    """
    taken: list[Path | None] = []

    @contextmanager
    def recording(workspace_root: Path | None = None, **kwargs: Any) -> Iterator[None]:
        taken.append(workspace_root)
        with shared_write_lock(workspace_root, **kwargs):
            yield

    monkeypatch.setattr(config_repository_module, "shared_write_lock", recording)
    return taken


def _config_dir(workspace: Path) -> Path:
    return workspace / ".guildbotics/config"


def _post_init(client: TestClient, workspace: Path) -> Any:
    """Send the first-setup write without judging the answer."""
    return client.post(
        "/config/init",
        headers=AUTH_HEADERS,
        json={
            "config_dir": str(_config_dir(workspace)),
            "language": "en",
            "description": "Temp automation workspace",
            "llm_api_type": "openai",
            "cli_agent": "codex",
            "provider_api_keys": {"openai": "test-openai-key"},
        },
    )


def _init(client: TestClient, workspace: Path) -> Path:
    response = _post_init(client, workspace)
    assert response.status_code == HTTP_OK
    return _config_dir(workspace)


def _add_member(client: TestClient, config_dir: Path) -> Any:
    response = _post_member(client, config_dir)
    assert response.status_code == HTTP_OK
    return response


def _post_member(client: TestClient, config_dir: Path) -> Any:
    return client.post(
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


def _add_command_file(client: TestClient) -> str:
    response = client.post(
        "/commands/files",
        headers=AUTH_HEADERS,
        json={"command": "demo", "format": "markdown", "content": "# demo\n"},
    )
    assert response.status_code == HTTP_OK
    return response.json()["id"]


def _command_file(client: TestClient) -> dict[str, str]:
    """The command file the update / delete mutations act on."""
    listing = client.get("/commands/files", headers=AUTH_HEADERS).json()["files"]
    entry = next(item for item in listing if item["command"].endswith("demo"))
    detail = client.get(f"/commands/files/{entry['id']}", headers=AUTH_HEADERS).json()
    return {"id": detail["id"], "revision": detail["revision"]}


#: How to exercise each config-writing route, keyed the way FastAPI names it.
MUTATIONS: dict[tuple[str, str], Callable[[TestClient, Path], Any]] = {
    ("POST", "/config/init"): lambda client, workspace: _post_init(client, workspace),
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
    ("POST", "/config/members"): lambda client, workspace: _post_member(
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
    # Commands are config files too; the prefix they are served under is not
    # what decides.
    ("POST", "/commands/files"): lambda client, workspace: client.post(
        "/commands/files",
        headers=AUTH_HEADERS,
        json={"command": "another", "format": "markdown", "content": "# x\n"},
    ),
    ("PUT", "/commands/files/{file_id}"): lambda client, workspace: client.put(
        f"/commands/files/{_command_file(client)['id']}",
        headers=AUTH_HEADERS,
        json={
            "content": "# edited\n",
            "expected_revision": _command_file(client)["revision"],
        },
    ),
    ("DELETE", "/commands/files/{file_id}"): lambda client, workspace: client.delete(
        f"/commands/files/{_command_file(client)['id']}"
        f"?expected_revision={_command_file(client)['revision']}",
        headers=AUTH_HEADERS,
    ),
    ("POST", "/commands/author/apply"): lambda client, workspace: client.post(
        "/commands/author/apply",
        headers=AUTH_HEADERS,
        json={
            "changes": [
                {
                    "operation": "create",
                    "command": "authored",
                    "format": "markdown",
                    "relative_path": "authored.md",
                    "content": "# authored\n",
                }
            ]
        },
    ),
    ("PUT", "/transcripts/settings"): lambda client, workspace: client.put(
        "/transcripts/settings",
        headers=AUTH_HEADERS,
        json={"detail": "full", "retention_days": 7},
    ),
}


def test_every_route_that_changes_anything_is_classified(client: TestClient) -> None:
    """A new route has to be sorted into one list or the other.

    The population is every mutating route, not the ones under ``/config``:
    drawing it around the URL is what let the command files and the transcript
    settings -- both config -- be written without comparing anything.
    """
    declared = set(MUTATIONS) | set(ELSEWHERE)
    routing_table = {
        (method, route.path)
        for route in client.app.routes  # type: ignore[attr-defined]
        for method in getattr(route, "methods", set())
        if method in {"POST", "PUT", "DELETE"}
    }

    assert routing_table == declared


@pytest.mark.parametrize(
    "route", sorted(MUTATIONS), ids=lambda route: f"{route[0]} {route[1]}"
)
def test_a_config_change_goes_through_the_comparing_writer(
    client: TestClient,
    workspace: Path,
    compared: list[Path | None],
    monkeypatch: pytest.MonkeyPatch,
    route: tuple[str, str],
) -> None:
    """Two screens can be open on the same config at once.

    A route that writes the file directly saves over whatever the other one
    wrote and answers as though nothing happened, because nothing compared the
    revisions the screen was composed against.
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
    if route[1].startswith("/commands/files/"):
        _add_command_file(client)
    compared.clear()

    MUTATIONS[route](client, workspace)

    assert compared, (
        f"{route[0]} {route[1]} wrote config without comparing what it read"
    )


async def _fake_url(*_args: object, **_kwargs: object) -> str:
    return "https://example.invalid/avatar.png"


async def _fake_download(_url: str) -> tuple[bytes, str]:
    return b"\x89PNG\r\n\x1a\n", ".png"


def _impatient(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    """Make ``module``'s lock give up at once instead of after 30 seconds."""

    @contextmanager
    def brief(workspace_root: Path | None = None, **_: Any) -> Iterator[None]:
        with shared_write_lock(workspace_root, timeout=0.05):
            yield

    monkeypatch.setattr(module, "shared_write_lock", brief)


@pytest.mark.parametrize(
    "route", sorted(MUTATIONS), ids=lambda route: f"{route[0]} {route[1]}"
)
def test_a_change_that_cannot_take_the_lock_is_a_503_rather_than_a_crash(
    client: TestClient,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: tuple[str, str],
) -> None:
    """Waiting on the other writer is not a failure of the request.

    A lock timeout is a ``TimeoutError``, so the family it belongs to would
    otherwise be read as the environment breaking. This is checked on every
    writer rather than one of them: a route that wraps its own body in a broad
    ``except`` turns the wait into its own 500, and only that route shows it.
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
    if route[1].startswith("/commands/files/"):
        _add_command_file(client)
    _impatient(monkeypatch, config_repository_module)

    with shared_write_lock(workspace):
        response = MUTATIONS[route](client, workspace)

    assert response is not None
    assert response.status_code == HTTP_SERVICE_UNAVAILABLE, response.text
    assert response.json()["code"] == "config_busy"
