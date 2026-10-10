from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.models import ConfigStatus
from guildbotics.integrations.github.app_manifest import (
    AppInstallation,
    AppManifestConversion,
)
from guildbotics.setup import github_app
from guildbotics.setup.setup_service import (
    GitHubUserReference,
    SimplePersonSetupService,
)
from guildbotics.utils.fileio import load_yaml_file
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.secret_store import KeyringSecretStore

HTTP_OK = 200
HTTP_FOUND = 302
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_SERVER_ERROR = 500
PEM = "-----BEGIN RSA PRIVATE KEY-----\nkey\n"

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}
CALLBACK_BASE = "http://testserver"


class RuntimeStub:
    def __init__(self, tmp_path: Path) -> None:
        self.config_status = ConfigStatus(
            cwd=tmp_path,
            workspace=tmp_path,
            config_dir=tmp_path / ".guildbotics/config",
            project_file=tmp_path / ".guildbotics/config/team/project.yml",
            project_file_exists=True,
            storage_dir=tmp_path,
        )

    def get_config_status(self) -> ConfigStatus:
        return self.config_status


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def fake_convert(code: str, *, transport=None) -> AppManifestConversion:
        assert code == "tmp-code"
        return AppManifestConversion(
            app_id=1978826,
            slug="my-bot",
            html_url="https://github.com/apps/my-bot",
            pem=PEM,
        )

    async def fake_list(app_id: str, pem: bytes, *, transport=None):
        return [AppInstallation(installation_id=86632391, account_login="acme")]

    monkeypatch.setattr(github_app.app_manifest, "convert_manifest_code", fake_convert)
    monkeypatch.setattr(github_app.app_manifest, "list_app_installations", fake_list)
    monkeypatch.setattr(
        SimplePersonSetupService,
        "resolve_github_user",
        lambda self, name, *, is_github_apps=False: GitHubUserReference(
            person_id=name,
            github_username=f"{name}[bot]",
            github_user_id=233270845,
            git_email=f"233270845+{name}[bot]@users.noreply.github.com",
        ),
    )
    team_dir = tmp_path / ".guildbotics/config/team"
    team_dir.mkdir(parents=True)
    (team_dir / "project.yml").write_text("language: en\n")
    return TestClient(
        create_app(session_token="secret", runtime=RuntimeStub(tmp_path)),  # type: ignore[arg-type]
        raise_server_exceptions=False,
    )


def _start_registration(client: TestClient) -> dict:
    response = client.post(
        "/config/members/github-app/registrations",
        json={
            "app_name": "my-bot",
            "person_id": "alice",
            "organization": "",
            "callback_base_url": CALLBACK_BASE,
        },
        headers=AUTH_HEADERS,
    )
    assert response.status_code == HTTP_OK
    return response.json()


def test_start_requires_session_token(client: TestClient) -> None:
    response = client.post(
        "/config/members/github-app/registrations",
        json={
            "app_name": "my-bot",
            "person_id": "alice",
            "callback_base_url": CALLBACK_BASE,
        },
    )
    assert response.status_code == HTTP_UNAUTHORIZED


def test_start_returns_state_and_start_url(client: TestClient) -> None:
    started = _start_registration(client)
    state = started["state"]
    assert started["status"] == "pending"
    assert started["start_url"] == (
        f"{CALLBACK_BASE}/github-app/registrations/{state}/start"
    )


def test_start_rejects_invalid_app_name(client: TestClient) -> None:
    response = client.post(
        "/config/members/github-app/registrations",
        json={
            "app_name": "x" * 35,
            "person_id": "alice",
            "callback_base_url": CALLBACK_BASE,
        },
        headers=AUTH_HEADERS,
    )
    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == "invalid_github_app_name"


def test_status_unknown_registration_returns_404(client: TestClient) -> None:
    response = client.get(
        "/config/members/github-app/registrations/missing", headers=AUTH_HEADERS
    )
    assert response.status_code == HTTP_NOT_FOUND


def test_start_page_posts_manifest_to_github(client: TestClient) -> None:
    started = _start_registration(client)
    response = client.get(f"/github-app/registrations/{started['state']}/start")
    assert response.status_code == HTTP_OK
    body = response.text
    assert (
        f'action="https://github.com/settings/apps/new?state={started["state"]}"'
        in body
    )
    assert 'name="manifest"' in body
    assert "my-bot" in body


def test_start_page_unknown_state_shows_error(client: TestClient) -> None:
    response = client.get("/github-app/registrations/missing/start")
    assert response.status_code == HTTP_BAD_REQUEST


def test_callback_converts_and_redirects_to_install_page(
    client: TestClient,
) -> None:
    started = _start_registration(client)
    response = client.get(
        "/github-app/registrations/callback",
        params={"code": "tmp-code", "state": started["state"]},
        follow_redirects=False,
    )
    assert response.status_code == HTTP_FOUND
    assert (
        response.headers["location"]
        == "https://github.com/apps/my-bot/installations/new"
    )


def test_callback_without_code_shows_error(client: TestClient) -> None:
    response = client.get(
        "/github-app/registrations/callback", params={"state": "whatever"}
    )
    assert response.status_code == HTTP_BAD_REQUEST


def test_status_reports_credentials_and_detected_installation(
    client: TestClient, tmp_path: Path
) -> None:
    started = _start_registration(client)
    client.get(
        "/github-app/registrations/callback",
        params={"code": "tmp-code", "state": started["state"]},
        follow_redirects=False,
    )
    response = client.get(
        f"/config/members/github-app/registrations/{started['state']}",
        headers=AUTH_HEADERS,
    )
    assert response.status_code == HTTP_OK
    status = response.json()
    assert status["status"] == "installed"
    assert status["slug"] == "my-bot"
    assert status["app_id"] == 1978826
    assert status["github_username"] == "my-bot[bot]"
    assert status["git_email"] == "233270845+my-bot[bot]@users.noreply.github.com"
    assert status["installation_id"] == 86632391
    assert status["installation_page_url"] == (
        "https://github.com/apps/my-bot/installations/new"
    )
    assert status["person_id"] == "alice"
    # The key stays in the Local API; the screen gets nothing to read it by.
    assert PEM not in response.text
    assert "private_key" not in response.text


def _installed_registration(client: TestClient) -> str:
    started = _start_registration(client)
    client.get(
        "/github-app/registrations/callback",
        params={"code": "tmp-code", "state": started["state"]},
        follow_redirects=False,
    )
    client.get(
        f"/config/members/github-app/registrations/{started['state']}",
        headers=AUTH_HEADERS,
    )
    return started["state"]


def _member(tmp_path: Path, **overrides) -> dict:
    return {
        "config_dir": str(tmp_path / ".guildbotics/config"),
        "person_type": "github_apps",
        "github_account_type": "github_apps",
        "person_id": "alice",
        "person_name": "Alice",
        "is_active": True,
        "github_username": "my-bot[bot]",
        "git_email": "233270845+my-bot[bot]@users.noreply.github.com",
        **overrides,
    }


def _add(client: TestClient, tmp_path: Path, **overrides):
    return client.post(
        "/config/members", json=_member(tmp_path, **overrides), headers=AUTH_HEADERS
    )


def _update(client: TestClient, tmp_path: Path, **overrides):
    return client.put(
        "/config/members/alice",
        json=_member(tmp_path, original_person_id="alice", **overrides),
        headers=AUTH_HEADERS,
    )


def _person_file(tmp_path: Path) -> Path:
    return tmp_path / ".guildbotics/config/team/members/alice/person.yml"


def _stored_key(tmp_path: Path) -> str | None:
    return KeyringSecretStore(tmp_path / ".guildbotics/config").get(
        "ALICE_GITHUB_PRIVATE_KEY"
    )


@pytest.mark.parametrize("save", [_add, _update])
def test_member_save_stores_the_registered_key_and_ids(
    client: TestClient, tmp_path: Path, save
) -> None:
    if save is _update:
        assert _add(client, tmp_path, person_type="", github_account_type="").is_success
    registration = _installed_registration(client)

    # What the screen sends for the app cannot pair with the registration's key.
    response = save(
        client,
        tmp_path,
        github_app_registration_id=registration,
        github_app_id=1,
        github_installation_id=2,
        github_private_key_path=str(tmp_path / "other.pem"),
    )

    assert response.status_code == HTTP_OK
    assert _stored_key(tmp_path) == PEM
    account = load_yaml_file(_person_file(tmp_path))["account_info"]
    assert account["github_app_id"] == "1978826"
    assert account["github_installation_id"] == "86632391"
    # A saved registration is gone: naming it again stores nothing.
    again = save(client, tmp_path, github_app_registration_id=registration)
    assert again.status_code == HTTP_BAD_REQUEST
    assert again.json()["code"] == "github_app_registration_not_found"


@pytest.mark.parametrize("save", [_add, _update])
@pytest.mark.parametrize(
    ("prepare", "code"),
    [
        (lambda client, monkeypatch: "missing", "github_app_registration_not_found"),
        (
            lambda client, monkeypatch: _start_registration(client)["state"],
            "github_app_registration_incomplete",
        ),
        (
            lambda client, monkeypatch: (
                _installed_registration(client),
                monkeypatch.setattr(github_app, "REGISTRATION_TTL_SECONDS", -1),
            )[0],
            "github_app_registration_not_found",
        ),
    ],
    ids=["missing", "incomplete", "expired"],
)
def test_member_save_refuses_a_registration_it_cannot_use_before_writing(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    save,
    prepare,
    code: str,
) -> None:
    if save is _update:
        assert _add(client, tmp_path, person_type="", github_account_type="").is_success
    before = _person_file(tmp_path).read_bytes() if save is _update else None

    response = save(
        client, tmp_path, github_app_registration_id=prepare(client, monkeypatch)
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == code
    assert _stored_key(tmp_path) is None
    if before is None:
        assert not _person_file(tmp_path).exists()
    else:
        assert _person_file(tmp_path).read_bytes() == before


def test_member_save_refuses_a_registration_for_another_member(
    client: TestClient, tmp_path: Path
) -> None:
    registration = _installed_registration(client)

    response = _add(
        client,
        tmp_path,
        person_id="bob",
        person_name="Bob",
        github_app_registration_id=registration,
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == "github_app_registration_member_mismatch"
    assert not (tmp_path / ".guildbotics/config/team/members/bob").exists()


def test_member_save_retries_a_registration_after_the_keychain_failed(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registration = _installed_registration(client)

    def locked(self, key, value):
        raise SecretStoreError("keychain is locked")

    with monkeypatch.context() as patch:
        patch.setattr(KeyringSecretStore, "set", locked)
        failed = _add(client, tmp_path, github_app_registration_id=registration)
    assert failed.status_code == HTTP_SERVER_ERROR
    assert not _person_file(tmp_path).exists()

    retried = _add(client, tmp_path, github_app_registration_id=registration)
    assert retried.status_code == HTTP_OK
    assert _stored_key(tmp_path) == PEM
