from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.models import ConfigStatus
from guildbotics.editions.simple import github_app_setup
from guildbotics.editions.simple.setup_service import (
    GitHubUserReference,
    SimplePersonSetupService,
)
from guildbotics.integrations.github.app_manifest import (
    AppInstallation,
    AppManifestConversion,
)

HTTP_OK = 200
HTTP_FOUND = 302
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}
CALLBACK_BASE = "http://testserver"


class RuntimeStub:
    def __init__(self, tmp_path: Path) -> None:
        self.config_status = ConfigStatus(
            cwd=tmp_path,
            workspace=tmp_path,
            config_dir=tmp_path / ".guildbotics/config",
            project_file=tmp_path / ".guildbotics/config/team/project.yml",
            project_file_exists=False,
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
            pem="-----BEGIN RSA PRIVATE KEY-----\nkey\n",
        )

    async def fake_list(app_id: str, pem: bytes, *, transport=None):
        return [AppInstallation(installation_id=86632391, account_login="acme")]

    monkeypatch.setattr(
        github_app_setup.app_manifest, "convert_manifest_code", fake_convert
    )
    monkeypatch.setattr(
        github_app_setup.app_manifest, "list_app_installations", fake_list
    )
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
    return TestClient(
        create_app(session_token="secret", runtime=RuntimeStub(tmp_path))  # type: ignore[arg-type]
    )


def _start_registration(client: TestClient) -> dict:
    response = client.post(
        "/config/members/github-app/registrations",
        json={
            "app_name": "my-bot",
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
        json={"app_name": "my-bot", "callback_base_url": CALLBACK_BASE},
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
        json={"app_name": "x" * 35, "callback_base_url": CALLBACK_BASE},
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
    key_file = Path(status["private_key_path"])
    from guildbotics.editions.simple.setup_service import github_app_key_dir

    assert key_file.is_relative_to(github_app_key_dir())
    assert key_file.read_text() == "-----BEGIN RSA PRIVATE KEY-----\nkey\n"
