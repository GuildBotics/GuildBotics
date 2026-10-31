"""The Slack App registration and the token check, as the member screen
reaches them through the App API."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from guildbotics.integrations.slack import app_manifest
from guildbotics.integrations.slack import setup as slack_setup
from tests.guildbotics.app_api.provider_doubles import (
    answer,
    route_web,
    saved_workspace,
    write_member,
)

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNPROCESSABLE = 422

BOT_TOKEN = "xoxb-secret-bot-token"
APP_TOKEN = "xapp-secret-app-token"
REGISTRATIONS = "/config/providers/slack/app-registrations"
VERIFY = "/config/providers/slack/verify-tokens"


@pytest.fixture
def client(workspace: Path) -> Iterator[TestClient]:
    with saved_workspace(workspace, {"chat_service": {"name": "slack"}}) as client:
        yield client


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """The project's Slack, accepting any token."""
    return route_web(
        monkeypatch,
        {
            "slack.com/api/auth.test": answer(
                {
                    "ok": True,
                    "user_id": "U0BOT",
                    "user": "alice-bot",
                    "team": "GuildBotics HQ",
                }
            ),
            "slack.com/api/conversations.list": answer({"ok": True, "channels": []}),
            "slack.com/api/apps.connections.open": answer(
                {"ok": True, "url": "wss://example.invalid"}
            ),
        },
    )


def _tokens(requests: list[httpx.Request]) -> dict[str, str]:
    return {
        request.url.path.rsplit("/", 1)[-1]: request.headers["Authorization"]
        for request in requests
    }


def test_start_returns_the_manifest_deep_link(client: TestClient) -> None:
    response = client.post(REGISTRATIONS, json={"app_name": "Alice Bot"})

    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["app_name"] == "Alice Bot"
    assert body["app_directory_url"] == "https://api.slack.com/apps"
    query = parse_qs(urlparse(body["registration_url"]).query)
    assert query["new_app"] == ["1"]
    assert json.loads(query["manifest_json"][0]) == app_manifest.build_app_manifest(
        "Alice Bot"
    )


@pytest.mark.parametrize(
    ("app_name", "status_code", "code"),
    [
        ("x" * 36, HTTP_BAD_REQUEST, "invalid_slack_app_name"),
        ("", HTTP_UNPROCESSABLE, "validation_error"),
    ],
)
def test_start_refuses_an_app_name_slack_cannot_take(
    client: TestClient, app_name: str, status_code: int, code: str
) -> None:
    response = client.post(REGISTRATIONS, json={"app_name": app_name})

    assert response.status_code == status_code
    assert response.json()["code"] == code


def test_verify_reports_bot_identity_and_workspace(
    client: TestClient, slack: list[httpx.Request]
) -> None:
    response = client.post(
        VERIFY,
        json={"bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    assert response.status_code == HTTP_OK
    assert response.json() == {
        "bot_ok": True,
        "bot_user_id": "U0BOT",
        "bot_display_name": "alice-bot",
        "workspace": "GuildBotics HQ",
        "bot_error": "",
        "bot_source": slack_setup.SOURCE_INPUT,
        "scopes_ok": True,
        "scope_error": "",
        "scope_needed": "",
        "app_token_ok": True,
        "app_token_error": "",
        "app_token_source": slack_setup.SOURCE_INPUT,
        "channels": [],
    }
    # Each token is checked by its own method.
    assert _tokens(slack) == {
        "auth.test": f"Bearer {BOT_TOKEN}",
        "conversations.list": f"Bearer {BOT_TOKEN}",
        "apps.connections.open": f"Bearer {APP_TOKEN}",
    }


def test_verify_checks_the_stored_tokens_of_the_member_being_edited(
    client: TestClient, workspace: Path, slack: list[httpx.Request]
) -> None:
    """Empty fields mean "keep the stored token", so they are not missing."""
    write_member(
        workspace,
        "alice",
        "person_type: agent\n",
        SLACK_BOT_TOKEN="xoxb-stored",
        SLACK_APP_TOKEN="xapp-stored",
    )

    response = client.post(
        VERIFY,
        json={"bot_token": "", "app_token": "", "person_id": "alice"},
    )

    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["bot_ok"] is True
    assert body["app_token_ok"] is True
    assert body["bot_source"] == slack_setup.SOURCE_STORED
    assert body["app_token_source"] == slack_setup.SOURCE_STORED
    assert _tokens(slack)["auth.test"] == "Bearer xoxb-stored"
    assert _tokens(slack)["apps.connections.open"] == "Bearer xapp-stored"
    assert "xoxb-stored" not in response.text


@pytest.mark.parametrize("person_id", ["", "bob"], ids=["new-member", "unknown"])
def test_verify_without_stored_tokens_reports_them_missing_without_calling_slack(
    client: TestClient, workspace: Path, slack: list[httpx.Request], person_id: str
) -> None:
    write_member(workspace, "alice", "person_type: agent\n", SLACK_BOT_TOKEN="xoxb-a")

    response = client.post(VERIFY, json={"person_id": person_id})

    body = response.json()
    assert body["bot_error"] == slack_setup.ERROR_MISSING
    assert body["app_token_error"] == slack_setup.ERROR_MISSING
    assert body["bot_source"] == slack_setup.SOURCE_NONE
    assert body["app_token_source"] == slack_setup.SOURCE_NONE
    assert slack == []


def test_verify_reports_slack_error_codes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = answer({"ok": False, "error": "invalid_auth"})
    route_web(
        monkeypatch,
        {
            "slack.com/api/auth.test": refused,
            "slack.com/api/apps.connections.open": refused,
        },
    )

    response = client.post(
        VERIFY,
        json={"bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )

    body = response.json()
    assert body["bot_ok"] is False
    assert body["bot_error"] == "invalid_auth"
    assert body["app_token_ok"] is False
    assert body["app_token_error"] == "invalid_auth"


@pytest.mark.usefixtures("slack")
def test_verify_never_echoes_or_logs_the_tokens(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            VERIFY,
            json={"bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
        )

    assert BOT_TOKEN not in response.text
    assert APP_TOKEN not in response.text
    assert BOT_TOKEN not in caplog.text
    assert APP_TOKEN not in caplog.text
