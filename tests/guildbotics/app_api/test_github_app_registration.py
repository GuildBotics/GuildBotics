"""The GitHub App registration, as the setup screens and the browser drive it
through the App API: the screen starts it and polls it with the session
token, the browser opens its pages without one, and the member save takes the
app's IDs and key from it."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from functools import cache
from html import unescape
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.models import ConfigStatus
from guildbotics.integrations.github import setup as github_setup
from guildbotics.utils.fileio import load_yaml_file
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.person_id import IDENTITY_FILE, identity_members_dir
from guildbotics.utils.secret_store import KeyringSecretStore, identity_secret_store
from tests.guildbotics.app_api.owner import ORIGIN, owner_client
from tests.guildbotics.app_api.provider_doubles import (
    answer,
    route_web,
)

HTTP_OK = 200
HTTP_FOUND = 302
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_SERVER_ERROR = 500
APP_ID = 1978826
INSTALLATION_ID = 86632391
BOT_USER_ID = 233270845
PAGES = "/providers/github/pages"


@cache
def _pem() -> str:
    """A real key: the installation check signs a JWT with it."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


class RuntimeStub:
    system_service_run_id = "stub-service"

    def __init__(self, tmp_path: Path) -> None:
        self.config_status = ConfigStatus(
            cwd=tmp_path,
            workspace=tmp_path,
            config_dir=tmp_path / ".guildbotics/config",
            project_file=tmp_path / ".guildbotics/config/team/project.yml",
            project_file_exists=True,
            storage_dir=tmp_path,
        )
        self.event_bus = EventBus()

    def get_config_status(self) -> ConfigStatus:
        return self.config_status

    def stop_scheduler(self, *, force: bool = False) -> None:
        pass

    async def close_cli_agent_usage(self) -> None:
        pass

    def saved_team(self) -> None:
        raise AssertionError("the registration reads no team")


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """GitHub: the manifest code converts, the app is installed once, and its
    bot user exists."""
    # Registrations live in the process; each test starts with none.
    monkeypatch.setattr(github_setup._registrations, "_registrations", {})
    return route_web(
        monkeypatch,
        {
            "api.github.com/app-manifests/tmp-code/conversions": answer(
                {
                    "id": APP_ID,
                    "slug": "my-bot",
                    "html_url": "https://github.com/apps/my-bot",
                    "pem": _pem(),
                    "owner": {"login": "acme"},
                },
                status_code=201,
            ),
            "api.github.com/app/installations": answer(
                [{"id": INSTALLATION_ID, "account": {"login": "acme"}}]
            ),
            "api.github.com/users/my-bot[bot]": answer(
                {"id": BOT_USER_ID, "login": "my-bot[bot]"}
            ),
        },
    )


@pytest.fixture
def client(tmp_path: Path, github: list[httpx.Request]) -> Iterator[TestClient]:
    team_dir = tmp_path / ".guildbotics/config/team"
    team_dir.mkdir(parents=True)
    (team_dir / "project.yml").write_text("language: en\n")
    with owner_client(
        create_app(runtime=RuntimeStub(tmp_path)),  # type: ignore[arg-type]
        raise_server_exceptions=False,
    ) as client:
        yield client


def _start(client: TestClient, person_id: str = "alice") -> dict[str, Any]:
    response = client.post(
        "/config/providers/github/app-registrations",
        json={
            "app_name": "my-bot",
            "person_id": person_id,
            "organization": "",
        },
    )
    assert response.status_code == HTTP_OK
    return response.json()


def _status(client: TestClient, state: str) -> httpx.Response:
    return client.post(
        "/config/providers/github/app-registration",
        json={"state": state},
    )


def _browser(client: TestClient) -> TestClient:
    """The user's browser coming back from GitHub, with no session cookie: a
    SameSite=Strict cookie is not sent on a navigation from another site."""
    return TestClient(client.app, base_url=ORIGIN)


def _callback(client: TestClient, state: str) -> httpx.Response:
    return _browser(client).get(
        f"{PAGES}/app-callback",
        params={"code": "tmp-code", "state": state},
        follow_redirects=False,
    )


def _installed(client: TestClient, person_id: str = "alice") -> str:
    state = _start(client, person_id)["state"]
    _callback(client, state)
    assert _status(client, state).json()["status"] == "installed"
    return state


def test_the_registration_from_start_to_installation(
    client: TestClient, github: list[httpx.Request]
) -> None:
    started = _start(client)
    state = started["state"]
    assert started["status"] == "pending"
    assert started["start_url"] == f"{ORIGIN}{PAGES}/app-manifest?state={state}"

    # The browser opens the start URL with no session, and the user's press
    # of its button posts the manifest to github.com: the page runs no script,
    # and its policy lets its form post there besides this server.
    manifest_page = _browser(client).get(started["start_url"])
    assert manifest_page.status_code == HTTP_OK
    assert "<script" not in manifest_page.text
    policy = manifest_page.headers["content-security-policy"]
    assert "form-action 'self' https://github.com;" in policy
    assert "script-src 'self';" in policy
    action = re.search(r'action="([^"]+)"', manifest_page.text)
    assert action is not None
    assert unescape(action.group(1)) == (
        f"https://github.com/settings/apps/new?state={state}"
    )
    value = re.search(r'name="manifest" value="([^"]+)"', manifest_page.text)
    assert value is not None
    manifest = json.loads(unescape(value.group(1)))
    assert manifest["name"] == "my-bot"
    assert manifest["redirect_url"] == f"{ORIGIN}{PAGES}/app-callback"

    # GitHub sends the browser back with a one-time code, converted here; the
    # browser goes on to install the app.
    callback = _callback(client, state)
    assert callback.status_code == HTTP_FOUND
    assert callback.headers["location"] == (
        "https://github.com/apps/my-bot/installations/new"
    )

    status = _status(client, state)
    assert status.status_code == HTTP_OK
    assert status.json() == {
        "state": state,
        "status": "installed",
        "app_name": "my-bot",
        "person_id": "alice",
        "slug": "my-bot",
        "app_id": APP_ID,
        "html_url": "https://github.com/apps/my-bot",
        "username": "my-bot[bot]",
        "git_email": f"{BOT_USER_ID}+my-bot[bot]@users.noreply.github.com",
        "installation_id": INSTALLATION_ID,
        "installation_check_error": "",
        "start_url": started["start_url"],
        "installation_page_url": "https://github.com/apps/my-bot/installations/new",
    }
    # The key stays in the Local API; the screen gets nothing to read it by.
    assert "PRIVATE KEY" not in status.text
    # The installations were listed as the app itself.
    listing = next(r for r in github if r.url.path == "/app/installations")
    assert listing.headers["Authorization"].startswith("Bearer ey")


def test_start_refuses_an_invalid_app_name(client: TestClient) -> None:
    response = client.post(
        "/config/providers/github/app-registrations",
        json={
            "app_name": "x" * 35,
            "person_id": "alice",
        },
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json() == {
        "code": "invalid_github_app_name",
        "message": "GitHub App name must be 1-34 characters.",
        "context": {},
    }


def test_status_of_an_unknown_registration_is_not_found(client: TestClient) -> None:
    response = _status(client, "missing")

    assert response.status_code == HTTP_NOT_FOUND
    assert response.json()["code"] == "github_app_registration_not_found"


@pytest.mark.parametrize(
    ("page", "params"),
    [
        ("app-manifest", {"state": "missing"}),
        ("app-callback", {"state": "missing", "code": "tmp-code"}),
        ("app-callback", {"state": "whatever"}),
    ],
    ids=["manifest-unknown", "callback-unknown", "callback-without-code"],
)
def test_a_page_that_cannot_go_on_tells_the_user_to_retry_from_the_app(
    client: TestClient, github: list[httpx.Request], page: str, params: dict
) -> None:
    response = _browser(client).get(f"{PAGES}/{page}", params=params)

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.headers["content-type"].startswith("text/html")
    assert github == []


# --- the member save ------------------------------------------------------


def _member(tmp_path: Path, **account: Any) -> dict[str, Any]:
    return {
        "config_dir": str(tmp_path / ".guildbotics/config"),
        "person_type": "agent",
        "person_id": "alice",
        "person_name": "Alice",
        "is_active": True,
        "git_email": f"{BOT_USER_ID}+my-bot[bot]@users.noreply.github.com",
        "accounts": {
            "github": {
                "account_type": "github_apps",
                "username": "my-bot[bot]",
                **account,
            }
        },
    }


def _add(client: TestClient, tmp_path: Path, **account: Any) -> httpx.Response:
    return client.post("/config/members", json=_member(tmp_path, **account))


def _update(client: TestClient, tmp_path: Path, **account: Any) -> httpx.Response:
    return client.put(
        "/config/members/alice",
        json=_member(tmp_path, **account),
    )


def _person_file(tmp_path: Path) -> Path:
    """Where the member's accounts are: its identity."""
    return identity_members_dir() / "alice" / IDENTITY_FILE


def _stored_key(tmp_path: Path) -> str | None:
    return identity_secret_store().get("ALICE_GITHUB_PRIVATE_KEY")


Save = Callable[..., httpx.Response]


@pytest.mark.parametrize("save", [_add, _update], ids=["add", "update"])
def test_member_save_stores_the_registered_key_and_ids(
    client: TestClient, tmp_path: Path, save: Save
) -> None:
    if save is _update:
        assert _add(client, tmp_path, account_type="").status_code == HTTP_OK
    registration = _installed(client)

    # What the form says of the app cannot pair with the registration's key.
    response = save(
        client,
        tmp_path,
        app_registration_id=registration,
        app_id=1,
        installation_id=2,
        private_key="not the registered key",
    )

    assert response.status_code == HTTP_OK
    assert _stored_key(tmp_path) == _pem()
    assert load_yaml_file(_person_file(tmp_path))["account_info"] == {
        "git_user": "Alice",
        "git_email": f"{BOT_USER_ID}+my-bot[bot]@users.noreply.github.com",
        "github_account_type": "github_apps",
        "github_username": "my-bot[bot]",
        "github_app_id": str(APP_ID),
        "github_installation_id": str(INSTALLATION_ID),
    }
    # The key never reaches the config, and the screen reads only that it is held.
    assert _pem() not in _person_file(tmp_path).read_text()
    view = client.get("/config/members/alice").json()
    assert view["accounts"]["github"]["has_private_key"] is True
    # A saved registration is gone: naming it again stores nothing.
    again = save(client, tmp_path, app_registration_id=registration)
    assert again.status_code == HTTP_BAD_REQUEST
    assert again.json()["code"] == "github_app_registration_not_found"
    assert _status(client, registration).status_code == HTTP_NOT_FOUND


def _missing(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    return "missing"


def _pending(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    return _start(client)["state"]


def _expired(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    state = _installed(client)
    monkeypatch.setattr(github_setup, "_REGISTRATION_TTL_SECONDS", -1)
    return state


def _for_bob(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    return _installed(client, person_id="bob")


def _in_another_workspace(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    with monkeypatch.context() as other:
        root = Path(os.environ["GUILDBOTICS_WORKSPACE_ROOT"]) / "other"
        other.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(root))
        return _installed(client)


@pytest.mark.parametrize("save", [_add, _update], ids=["add", "update"])
@pytest.mark.parametrize(
    ("registration", "code"),
    [
        (_missing, "github_app_registration_not_found"),
        (_pending, "github_app_registration_incomplete"),
        (_expired, "github_app_registration_not_found"),
        (_for_bob, "github_app_registration_member_mismatch"),
        (_in_another_workspace, "github_app_registration_not_found"),
    ],
    ids=["missing", "incomplete", "expired", "another-member", "another-workspace"],
)
def test_member_save_refuses_a_registration_it_cannot_use_before_writing(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    save: Save,
    registration: Callable[[TestClient, pytest.MonkeyPatch], str],
    code: str,
) -> None:
    if save is _update:
        assert _add(client, tmp_path, account_type="").status_code == HTTP_OK
    before = _person_file(tmp_path).read_bytes() if save is _update else None

    response = save(
        client, tmp_path, app_registration_id=registration(client, monkeypatch)
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == code
    assert _stored_key(tmp_path) is None
    if before is None:
        assert not _person_file(tmp_path).exists()
    else:
        assert _person_file(tmp_path).read_bytes() == before


def test_member_save_retries_a_registration_after_the_keychain_failed(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registration = _installed(client)

    def locked(self: KeyringSecretStore, key: str, value: str) -> None:
        raise SecretStoreError("keychain is locked")

    with monkeypatch.context() as patch:
        patch.setattr(KeyringSecretStore, "set", locked)
        failed = _add(client, tmp_path, app_registration_id=registration)
    assert failed.status_code == HTTP_SERVER_ERROR
    assert not _person_file(tmp_path).exists()

    retried = _add(client, tmp_path, app_registration_id=registration)
    assert retried.status_code == HTTP_OK
    assert _stored_key(tmp_path) == _pem()
