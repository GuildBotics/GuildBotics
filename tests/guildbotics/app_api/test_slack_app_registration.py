import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.models import ConfigStatus
from guildbotics.editions.simple import slack_app_setup
from guildbotics.editions.simple.setup_service import SimplePersonSetupService
from guildbotics.integrations.slack import app_manifest

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_UNPROCESSABLE = 422

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}
BOT_TOKEN = "xoxb-secret-bot-token"
APP_TOKEN = "xapp-secret-app-token"


class RuntimeStub:
    def __init__(self, tmp_path: Path) -> None:
        self.config_status = ConfigStatus(
            cwd=tmp_path,
            env_file=tmp_path / ".env",
            env_file_exists=False,
            config_dir=tmp_path / ".guildbotics/config",
            project_file=tmp_path / ".guildbotics/config/team/project.yml",
            project_file_exists=False,
            storage_dir=tmp_path / ".guildbotics/data",
        )

    def get_config_status(self) -> ConfigStatus:
        return self.config_status


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(session_token="secret", runtime=RuntimeStub(tmp_path))  # type: ignore[arg-type]
    )


@pytest.fixture
def slack_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth.test"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "user_id": "U0BOT",
                    "user": "alice-bot",
                    "team": "GuildBotics HQ",
                },
            )
        return httpx.Response(200, json={"ok": True, "url": "wss://example.invalid"})

    _patch_transport(monkeypatch, httpx.MockTransport(handler))


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport
) -> None:
    original = slack_app_setup.verify_tokens

    async def verify(bot_token: str, app_token: str, **kwargs):
        kwargs["transport"] = transport
        return await original(bot_token, app_token, **kwargs)

    monkeypatch.setattr(slack_app_setup, "verify_tokens", verify)


def test_start_requires_session_token(client: TestClient) -> None:
    response = client.post(
        "/config/members/slack-app/registrations", json={"app_name": "alice"}
    )

    assert response.status_code == HTTP_UNAUTHORIZED


def test_start_returns_the_manifest_deep_link(client: TestClient) -> None:
    response = client.post(
        "/config/members/slack-app/registrations",
        json={"app_name": "Alice Bot"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["app_name"] == "Alice Bot"
    assert body["app_directory_url"] == "https://api.slack.com/apps"
    query = parse_qs(urlparse(body["registration_url"]).query)
    assert query["new_app"] == ["1"]
    assert json.loads(query["manifest_json"][0]) == app_manifest.build_app_manifest(
        "Alice Bot"
    )


def test_start_rejects_a_too_long_app_name(client: TestClient) -> None:
    response = client.post(
        "/config/members/slack-app/registrations",
        json={"app_name": "x" * 36},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["code"] == "invalid_slack_app_name"


def test_start_rejects_an_empty_app_name(client: TestClient) -> None:
    response = client.post(
        "/config/members/slack-app/registrations",
        json={"app_name": ""},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == HTTP_UNPROCESSABLE


def test_verify_requires_session_token(client: TestClient) -> None:
    response = client.post(
        "/config/members/slack-app/verify",
        json={"bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == HTTP_UNAUTHORIZED


@pytest.mark.usefixtures("slack_ok")
def test_verify_reports_bot_identity_and_workspace(client: TestClient) -> None:
    response = client.post(
        "/config/members/slack-app/verify",
        json={"bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == HTTP_OK
    assert response.json() == {
        "bot_ok": True,
        "bot_user_id": "U0BOT",
        "bot_display_name": "alice-bot",
        "workspace": "GuildBotics HQ",
        "bot_error": "",
        "bot_source": slack_app_setup.SOURCE_INPUT,
        "scopes_ok": True,
        "scope_error": "",
        "scope_needed": "",
        "app_token_ok": True,
        "app_token_error": "",
        "app_token_source": slack_app_setup.SOURCE_INPUT,
        "channels": [],
    }


def test_verify_reports_slack_error_codes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    _patch_transport(monkeypatch, httpx.MockTransport(handler))

    response = client.post(
        "/config/members/slack-app/verify",
        json={"bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
        headers=AUTH_HEADERS,
    )

    body = response.json()
    assert body["bot_ok"] is False
    assert body["bot_error"] == "invalid_auth"
    assert body["app_token_ok"] is False
    assert body["app_token_error"] == "invalid_auth"


def test_verify_defaults_missing_tokens_to_a_missing_result(client: TestClient) -> None:
    response = client.post(
        "/config/members/slack-app/verify", json={}, headers=AUTH_HEADERS
    )

    body = response.json()
    assert body["bot_error"] == slack_app_setup.ERROR_MISSING
    assert body["app_token_error"] == slack_app_setup.ERROR_MISSING
    assert body["bot_source"] == slack_app_setup.SOURCE_NONE
    assert body["app_token_source"] == slack_app_setup.SOURCE_NONE


@pytest.mark.usefixtures("slack_ok")
def test_verify_checks_the_stored_tokens_of_the_member_being_edited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty fields mean "keep the stored token", so they are not missing."""
    seen: dict = {}

    def read_slack_tokens(self, *, config_dir, person_id, env_file_path):
        seen["person_id"] = person_id
        return (BOT_TOKEN, APP_TOKEN)

    monkeypatch.setattr(
        SimplePersonSetupService, "read_slack_tokens", read_slack_tokens
    )

    response = client.post(
        "/config/members/slack-app/verify",
        json={"bot_token": "", "app_token": "", "person_id": "alice"},
        headers=AUTH_HEADERS,
    )

    assert seen["person_id"] == "alice"
    body = response.json()
    assert body["bot_ok"] is True
    assert body["app_token_ok"] is True
    assert body["bot_source"] == slack_app_setup.SOURCE_STORED
    assert body["app_token_source"] == slack_app_setup.SOURCE_STORED


def test_verify_does_not_read_stored_tokens_without_a_member(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args, **kwargs):  # pragma: no cover
        raise AssertionError("no member is being edited")

    monkeypatch.setattr(SimplePersonSetupService, "read_slack_tokens", fail)

    response = client.post(
        "/config/members/slack-app/verify", json={}, headers=AUTH_HEADERS
    )

    assert response.json()["bot_error"] == slack_app_setup.ERROR_MISSING


@pytest.mark.usefixtures("slack_ok")
def test_verify_never_echoes_or_logs_the_tokens(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/config/members/slack-app/verify",
            json={"bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
            headers=AUTH_HEADERS,
        )

    assert BOT_TOKEN not in response.text
    assert APP_TOKEN not in response.text
    assert BOT_TOKEN not in caplog.text
    assert APP_TOKEN not in caplog.text
