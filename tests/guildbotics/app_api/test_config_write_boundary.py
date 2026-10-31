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
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import guildbotics.app_api.avatar as avatar_module
import guildbotics.workspace.config_repository as config_repository_module
from guildbotics.integrations.factory import PROVIDERS
from guildbotics.utils.fileio import get_identity_root
from guildbotics.utils.local_api import API_PREFIX
from guildbotics.utils.shared_write_lock import shared_write_lock

HTTP_OK = 200
HTTP_SERVICE_UNAVAILABLE = 503


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
    ("POST", "/member/plan"): "parses Click inputs without executing a command",
    (
        "POST",
        "/member/run",
    ): "member capabilities write state/ or services; no edited config snapshot",
    (
        "POST",
        "/member/git",
    ): "exchanges bundles in private Git repositories and records interactive work",
    (
        "DELETE",
        "/device/workspaces",
    ): "device-local workspace registry; no workspace config",
    (
        "POST",
        "/intelligences/decisions/credential",
    ): "OS secret store and atomic index update; no edited config snapshot",
    ("POST", "/chat/receive-state/reset"): "state/, settled by first-committer-wins",
    ("POST", "/commands/author"): "proposes a change set, writes nothing",
    ("POST", "/commands/input-files"): "a temporary file outside the workspace",
    ("POST", "/commands/input-files/copy"): "a temporary file outside the workspace",
    (
        "POST",
        "/commands/input-paths",
    ): "answers what a turn would reach, writes nothing",
    ("POST", "/commands/run"): "starts a run; its own writes go through the port",
    ("POST", "/diagnostics/scenario"): "local/run diagnostics",
    ("POST", "/diagnostics/troubleshoot"): "local/run diagnostics",
    ("POST", "/hub"): "~/.guildbotics/hub, outside any workspace",
    (
        "POST",
        "/intelligences/agent-environment/build",
    ): "local/agent_environment snapshots, device-local",
    ("POST", "/hub/inspect"): "reads a hub",
    ("POST", "/hub/secrets/{workspace_id}/{operation}"): (
        "Hub keychain and generation index, outside any workspace; secrets.lock"
    ),
    ("POST", "/hub/ssh-key"): "~/.ssh",
    ("POST", "/hub/trust"): "~/.ssh/known_hosts",
    ("POST", "/scheduler/start"): "runtime lifecycle",
    ("POST", "/scheduler/stop"): "runtime lifecycle",
    ("POST", "/shutdown"): "runtime lifecycle",
    ("POST", "/local-api/proof"): "computes a proof, writes nothing",
    ("POST", "/session/links"): "an entry code held in memory",
    ("POST", "/session"): "the server's own records, outside any workspace",
    ("POST", "/apps/authorizations"): "a consent code held in memory",
    ("POST", "/apps/token"): "the server's own records, outside any workspace",
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
    ("POST", "/identities/secrets/fetch"): "OS secret stores; writes no config",
    (
        "POST",
        "/identities/secrets/send",
    ): "one key generation, read-modify-written inside the port's lock",
    (
        "POST",
        "/identities/sync/rejections/{rejection_id}/discard",
    ): "deletes a local ref, writes no config",
    ("POST", "/identities/sync/retry"): "the identities' whole repository",
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
    ("PUT", "/service/keep-awake"): "local/service.yml is device-specific by design",
}

#: What each provider's setup operation does instead of writing shared config.
#: A provider's operations are routes like any other, so a new one has to be
#: classified here too; none of them write config, because what the setup
#: screens learn from a provider reaches config only through a save.
PROVIDER_OPERATIONS = {
    ("github", "resolve-user"): "reads a GitHub user",
    ("github", "status-options"): "reads a Projects board's lanes",
    ("github", "agent-field"): "reads a Projects board's Agent field",
    ("github", "agent-field/ensure"): "adds options to the board's field on GitHub",
    ("github", "app-registrations"): (
        "starts a GitHub App registration held in memory"
    ),
    ("github", "app-registration"): "polls a registration held in memory",
    ("branvil", "connect"): "starts a consent held in memory",
    ("branvil", "connection"): "reads the admin token's owner on Branvil",
    ("branvil", "workspaces"): "lists Branvil's workspaces",
    ("branvil", "workspaces/create"): "creates a workspace on Branvil",
    ("branvil", "agents"): "lists Branvil's agents",
    ("branvil", "entry"): "asks Branvil for an entry link",
    ("slack", "app-registrations"): "builds a Slack App deep link",
    ("slack", "verify-tokens"): "calls Slack with the entered or stored tokens",
}
ELSEWHERE |= {
    ("POST", f"/config/providers/{provider}/{operation}"): reason
    for (provider, operation), reason in PROVIDER_OPERATIONS.items()
}


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
        json={
            "config_dir": str(config_dir),
            "person_type": "",
            "person_id": "alice",
            "person_name": "Alice",
            "is_active": True,
            # The avatar imports need something to look the member up by.
            "accounts": {"github": {"username": "alice-gh"}},
            "git_email": "",
            "roles": ["architect"],
            "speaking_style": "concise",
        },
    )


def _add_command_file(client: TestClient) -> str:
    response = client.post(
        "/commands/files",
        json={"command": "demo", "format": "markdown", "content": "# demo\n"},
    )
    assert response.status_code == HTTP_OK
    return response.json()["id"]


def _command_file(client: TestClient) -> dict[str, str]:
    """The command file the update / delete mutations act on."""
    listing = client.get("/commands/files").json()["files"]
    entry = next(item for item in listing if item["command"].endswith("demo"))
    detail = client.get(f"/commands/files/{entry['id']}").json()
    return {"id": detail["id"], "revision": detail["revision"]}


#: How to exercise each config-writing route, keyed the way FastAPI names it.
MUTATIONS: dict[tuple[str, str], Callable[[TestClient, Path], Any]] = {
    ("POST", "/config/init"): lambda client, workspace: _post_init(client, workspace),
    ("PUT", "/config/project"): lambda client, workspace: client.put(
        "/config/project",
        json={
            "config_dir": str(_config_dir(workspace)),
            "language": "en",
            "description": "Renamed",
            "llm_api_type": "openai",
            "cli_agent": "codex",
        },
    ),
    ("PUT", "/config/project/default-person"): lambda client, workspace: client.put(
        "/config/project/default-person",
        json={"person_id": "alice"},
    ),
    ("POST", "/config/members"): lambda client, workspace: _post_member(
        client, _config_dir(workspace)
    ),
    ("PUT", "/config/members/{person_id}"): lambda client, workspace: client.put(
        "/config/members/alice",
        json={
            "config_dir": str(_config_dir(workspace)),
            "person_type": "",
            "person_id": "alice",
            "person_name": "Renamed",
            "is_active": True,
            "git_email": "",
            "roles": ["architect"],
            "speaking_style": "concise",
        },
    ),
    ("DELETE", "/config/members/{person_id}"): lambda client, workspace: client.delete(
        "/config/members/alice"
    ),
    ("DELETE", "/identities/{person_id}"): lambda client, workspace: client.delete(
        "/identities/alice"
    ),
    ("PUT", "/config/intelligences"): lambda client, workspace: client.put(
        "/config/intelligences",
        json={"config_dir": str(_config_dir(workspace)), "person_id": None},
    ),
    ("POST", "/config/members/{person_id}/avatar"): lambda client, workspace: (
        client.post(
            "/config/members/alice/avatar",
            files={"file": ("avatar.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
    ),
    # Commands are config files too; the prefix they are served under is not
    # what decides.
    ("POST", "/commands/files"): lambda client, workspace: client.post(
        "/commands/files",
        json={"command": "another", "format": "markdown", "content": "# x\n"},
    ),
    ("PUT", "/commands/files/{file_id}"): lambda client, workspace: client.put(
        f"/commands/files/{_command_file(client)['id']}",
        json={
            "content": "# edited\n",
            "expected_revision": _command_file(client)["revision"],
        },
    ),
    ("DELETE", "/commands/files/{file_id}"): lambda client, workspace: client.delete(
        f"/commands/files/{_command_file(client)['id']}"
        f"?expected_revision={_command_file(client)['revision']}",
    ),
    ("POST", "/commands/author/apply"): lambda client, workspace: client.post(
        "/commands/author/apply",
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
        json={"detail": "full", "retention_days": 7},
    ),
}


AVATAR_IMPORT = ("POST", "/config/members/{person_id}/avatar/{provider}")

#: The avatar import is one route, but each provider that has an avatar
#: answers it through its own setup, so each is exercised.
AVATAR_PROVIDERS = sorted(
    name
    for name, provider in PROVIDERS.items()
    if provider.setup is not None and provider.setup.avatar_url is not None
)


def _import_avatar(provider: str) -> Callable[[TestClient, Path], Any]:
    return lambda client, workspace: client.post(
        f"/config/members/alice/avatar/{provider}"
    )


CASES = [
    pytest.param(route, mutation, id=f"{route[0]} {route[1]}")
    for route, mutation in sorted(MUTATIONS.items())
] + [
    pytest.param(
        AVATAR_IMPORT,
        _import_avatar(provider),
        id=f"{AVATAR_IMPORT[0]} {AVATAR_IMPORT[1]} {provider}",
    )
    for provider in AVATAR_PROVIDERS
]


def test_every_route_that_changes_anything_is_classified(client: TestClient) -> None:
    """A new route has to be sorted into one list or the other.

    The population is every mutating route, not the ones under ``/config``:
    drawing it around the URL is what let the command files and the transcript
    settings -- both config -- be written without comparing anything. The
    routes are named as the API names them, under its prefix.
    """
    declared = {
        (method, API_PREFIX + path)
        for method, path in set(MUTATIONS) | set(ELSEWHERE) | {AVATAR_IMPORT}
    }
    routing_table = {
        (method, route.path)
        for route in client.app.routes  # type: ignore[attr-defined]
        for method in getattr(route, "methods", set())
        if method in {"POST", "PUT", "DELETE"}
    }

    assert routing_table == declared


@pytest.mark.parametrize(("route", "mutation"), CASES)
def test_a_config_change_goes_through_the_comparing_writer(
    client: TestClient,
    workspace: Path,
    compared: list[Path | None],
    monkeypatch: pytest.MonkeyPatch,
    route: tuple[str, str],
    mutation: Callable[[TestClient, Path], Any],
) -> None:
    """Two screens can be open on the same config at once.

    A route that writes the file directly saves over whatever the other one
    wrote and answers as though nothing happened, because nothing compared the
    revisions the screen was composed against.
    """
    _fake_avatars(monkeypatch)
    config_dir = _init(client, workspace)
    if route != ("POST", "/config/members"):
        _add_member(client, config_dir)
    if route[1].startswith("/commands/files/"):
        _add_command_file(client)
    compared.clear()

    mutation(client, workspace)

    assert compared, (
        f"{route[0]} {route[1]} wrote config without comparing what it read"
    )


def _fake_avatars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every provider's avatar is found and downloaded without the network."""
    for name in AVATAR_PROVIDERS:
        provider = PROVIDERS[name]
        assert provider.setup is not None
        monkeypatch.setitem(
            PROVIDERS,
            name,
            replace(provider, setup=replace(provider.setup, avatar_url=_fake_url)),
        )
    monkeypatch.setattr(avatar_module, "download_avatar", _fake_download)


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


@pytest.mark.parametrize(("route", "mutation"), CASES)
def test_a_change_that_cannot_take_the_lock_is_a_503_rather_than_a_crash(
    client: TestClient,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: tuple[str, str],
    mutation: Callable[[TestClient, Path], Any],
) -> None:
    """Waiting on the other writer is not a failure of the request.

    A lock timeout is a ``TimeoutError``, so the family it belongs to would
    otherwise be read as the environment breaking. This is checked on every
    writer rather than one of them: a route that wraps its own body in a broad
    ``except`` turns the wait into its own 500, and only that route shows it.
    """
    _fake_avatars(monkeypatch)
    config_dir = _init(client, workspace)
    if route != ("POST", "/config/members"):
        _add_member(client, config_dir)
    if route[1].startswith("/commands/files/"):
        _add_command_file(client)
    _impatient(monkeypatch, config_repository_module)

    # A member's identity is written under the identities' lock, which every
    # span that takes both takes first.
    with shared_write_lock(get_identity_root()), shared_write_lock(workspace):
        response = mutation(client, workspace)

    assert response is not None
    assert response.status_code == HTTP_SERVICE_UNAVAILABLE, response.text
    assert response.json()["code"] == "config_busy"
