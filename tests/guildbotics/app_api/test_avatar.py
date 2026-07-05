from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.models import ConfigStatus
from guildbotics.editions.simple.setup_service import PersonConfigSnapshot

AUTH_HEADERS = {"X-GuildBotics-Session-Token": "secret"}


class RuntimeStub:
    def __init__(self, tmp_path: Path) -> None:
        self.config_status = ConfigStatus(
            cwd=tmp_path,
            env_file=tmp_path / ".env",
            env_file_exists=True,
            config_dir=tmp_path / ".guildbotics/config",
            project_file=tmp_path / ".guildbotics/config/team/project.yml",
            project_file_exists=True,
            storage_dir=tmp_path / "home/.guildbotics/data",
        )

    def stop_scheduler(self, *, force: bool = False) -> None:
        pass

    def get_config_status(self) -> ConfigStatus:
        return self.config_status


@pytest.fixture
def test_workspace(tmp_path: Path) -> Path:
    # Setup standard member directory structure
    member_dir = tmp_path / ".guildbotics/config/team/members/alice"
    member_dir.mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def client(test_workspace: Path) -> TestClient:
    runtime = RuntimeStub(test_workspace)
    return TestClient(create_app(session_token="secret", runtime=runtime))


def test_get_avatar_not_found(client: TestClient) -> None:
    response = client.get("/config/members/alice/avatar", headers=AUTH_HEADERS)
    assert response.status_code == 404
    assert response.json()["code"] == "avatar_not_found"


def test_get_avatar_found(client: TestClient, test_workspace: Path) -> None:
    avatar_file = test_workspace / ".guildbotics/config/team/members/alice/avatar.png"
    avatar_file.write_bytes(b"fake image data")

    response = client.get("/config/members/alice/avatar", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.content == b"fake image data"
    assert response.headers.get("Cache-Control") == "no-cache"


def test_get_avatar_with_query_token(client: TestClient, test_workspace: Path) -> None:
    # <img> tags cannot send custom headers, so the token is accepted via query.
    avatar_file = test_workspace / ".guildbotics/config/team/members/alice/avatar.png"
    avatar_file.write_bytes(b"fake image data")

    response = client.get("/config/members/alice/avatar?token=secret")
    assert response.status_code == 200
    assert response.content == b"fake image data"


def test_get_avatar_rejects_missing_token(
    client: TestClient, test_workspace: Path
) -> None:
    avatar_file = test_workspace / ".guildbotics/config/team/members/alice/avatar.png"
    avatar_file.write_bytes(b"fake image data")

    response = client.get("/config/members/alice/avatar")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_session_token"

    response = client.get("/config/members/alice/avatar?token=wrong")
    assert response.status_code == 401


def test_post_avatar_upload(client: TestClient, test_workspace: Path) -> None:
    file_data = b"uploaded image content"
    files = {"file": ("avatar.png", io.BytesIO(file_data), "image/png")}

    response = client.post(
        "/config/members/alice/avatar", files=files, headers=AUTH_HEADERS
    )
    assert response.status_code == 200

    saved_avatar = test_workspace / ".guildbotics/config/team/members/alice/avatar.png"
    assert saved_avatar.exists()
    assert saved_avatar.read_bytes() == file_data
    # The response carries the saved file mtime as a deterministic cache-buster.
    assert response.json()["avatar_timestamp"] == int(saved_avatar.stat().st_mtime)


def test_post_avatar_upload_rejects_large_file(client: TestClient) -> None:
    from guildbotics.app_api.avatar import MAX_AVATAR_BYTES

    oversized = b"x" * (MAX_AVATAR_BYTES + 1)
    files = {"file": ("avatar.png", io.BytesIO(oversized), "image/png")}

    response = client.post(
        "/config/members/alice/avatar", files=files, headers=AUTH_HEADERS
    )
    assert response.status_code == 400
    assert response.json()["code"] == "avatar_invalid"


@patch("guildbotics.app_api.avatar.httpx.AsyncClient")
@patch(
    "guildbotics.editions.simple.setup_service.SimplePersonSetupService.read_person_config"
)
def test_import_avatar_from_github(
    mock_read_config: MagicMock,
    mock_async_client_cls: MagicMock,
    client: TestClient,
    test_workspace: Path,
) -> None:
    # Mock member config
    mock_read_config.return_value = PersonConfigSnapshot(
        person_id="alice",
        person_name="Alice",
        person_type="human",
        is_active=True,
        github_username="alice-git",
        git_email="alice@example.com",
    )

    # Mock HTTP response for user info and image download
    mock_client = MagicMock()
    mock_async_client_cls.return_value.__aenter__.return_value = mock_client

    mock_user_response = MagicMock()
    mock_user_response.json.return_value = {
        "avatar_url": "https://github.com/avatar/alice.png"
    }
    mock_user_response.raise_for_status = MagicMock()

    mock_image_response = MagicMock()
    mock_image_response.headers = {"Content-Type": "image/png"}
    mock_image_response.content = b"github image binary"
    mock_image_response.raise_for_status = MagicMock()

    mock_client.get = AsyncMock(side_effect=[mock_user_response, mock_image_response])

    response = client.post("/config/members/alice/avatar/github", headers=AUTH_HEADERS)
    assert response.status_code == 200

    # Assert get calls
    assert mock_client.get.call_count == 2
    mock_client.get.assert_any_call(
        "https://api.github.com/users/alice-git",
        headers={"User-Agent": "GuildBotics-App"},
        timeout=10.0,
    )
    mock_client.get.assert_any_call(
        "https://github.com/avatar/alice.png", follow_redirects=True, timeout=15.0
    )

    # Verify image saved
    saved_avatar = test_workspace / ".guildbotics/config/team/members/alice/avatar.png"
    assert saved_avatar.exists()
    assert saved_avatar.read_bytes() == b"github image binary"
    assert response.json()["avatar_timestamp"] == int(saved_avatar.stat().st_mtime)


@patch("guildbotics.app_api.avatar.httpx.AsyncClient")
@patch(
    "guildbotics.editions.simple.setup_service.SimplePersonSetupService.read_person_config"
)
def test_import_avatar_from_github_error_message_is_stable(
    mock_read_config: MagicMock,
    mock_async_client_cls: MagicMock,
    client: TestClient,
) -> None:
    mock_read_config.return_value = PersonConfigSnapshot(
        person_id="alice",
        person_name="Alice",
        person_type="human",
        is_active=True,
        github_username="alice-git",
        git_email="alice@example.com",
    )
    mock_client = MagicMock()
    mock_async_client_cls.return_value.__aenter__.return_value = mock_client
    # Raw exception text must not leak into the client-facing message.
    mock_client.get = AsyncMock(side_effect=RuntimeError("secret internal detail"))

    response = client.post("/config/members/alice/avatar/github", headers=AUTH_HEADERS)
    assert response.status_code == 500
    payload = response.json()
    assert payload["code"] == "avatar_import_failed"
    assert payload["message"] == "Failed to import avatar from GitHub."
    assert "secret internal detail" not in payload["message"]


@patch("guildbotics.app_api.api.dotenv_values")
@patch("guildbotics.app_api.avatar.httpx.AsyncClient")
@patch(
    "guildbotics.editions.simple.setup_service.SimplePersonSetupService.read_person_config"
)
def test_import_avatar_from_slack(
    mock_read_config: MagicMock,
    mock_async_client_cls: MagicMock,
    mock_dotenv: MagicMock,
    client: TestClient,
    test_workspace: Path,
) -> None:
    # Mock member config
    mock_read_config.return_value = PersonConfigSnapshot(
        person_id="alice",
        person_name="Alice",
        person_type="human",
        is_active=True,
        github_username="alice-git",
        git_email="alice@example.com",
        slack_user_id="U12345",
    )

    (test_workspace / ".env").touch()

    # Mock environment file values
    mock_dotenv.return_value = {"ALICE_SLACK_BOT_TOKEN": "xoxb-alice-token"}

    # Mock HTTP response for user info and image download
    mock_client = MagicMock()
    mock_async_client_cls.return_value.__aenter__.return_value = mock_client

    mock_user_response = MagicMock()
    mock_user_response.json.return_value = {
        "ok": True,
        "user": {"profile": {"image_512": "https://slack.com/avatar/alice.png"}},
    }
    mock_user_response.raise_for_status = MagicMock()

    mock_image_response = MagicMock()
    mock_image_response.headers = {"Content-Type": "image/png"}
    mock_image_response.content = b"slack image binary"
    mock_image_response.raise_for_status = MagicMock()

    mock_client.get = AsyncMock(side_effect=[mock_user_response, mock_image_response])

    response = client.post("/config/members/alice/avatar/slack", headers=AUTH_HEADERS)
    assert response.status_code == 200

    # Verify calls
    assert mock_client.get.call_count == 2
    mock_client.get.assert_any_call(
        "https://slack.com/api/users.info",
        params={"user": "U12345"},
        headers={"Authorization": "Bearer xoxb-alice-token"},
        timeout=10.0,
    )

    # Verify image saved
    saved_avatar = test_workspace / ".guildbotics/config/team/members/alice/avatar.png"
    assert saved_avatar.exists()
    assert saved_avatar.read_bytes() == b"slack image binary"


@patch("guildbotics.app_api.api.dotenv_values")
@patch("guildbotics.app_api.avatar.httpx.AsyncClient")
@patch(
    "guildbotics.editions.simple.setup_service.SimplePersonSetupService.read_person_config"
)
def test_import_avatar_from_slack_missing_scope(
    mock_read_config: MagicMock,
    mock_async_client_cls: MagicMock,
    mock_dotenv: MagicMock,
    client: TestClient,
    test_workspace: Path,
) -> None:
    mock_read_config.return_value = PersonConfigSnapshot(
        person_id="alice",
        person_name="Alice",
        person_type="human",
        is_active=True,
        github_username="alice-git",
        git_email="alice@example.com",
        slack_user_id="U12345",
    )

    (test_workspace / ".env").touch()
    mock_dotenv.return_value = {"ALICE_SLACK_BOT_TOKEN": "xoxb-alice-token"}

    mock_client = MagicMock()
    mock_async_client_cls.return_value.__aenter__.return_value = mock_client

    mock_user_response = MagicMock()
    mock_user_response.json.return_value = {"ok": False, "error": "missing_scope"}
    mock_user_response.raise_for_status = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_user_response)

    response = client.post("/config/members/alice/avatar/slack", headers=AUTH_HEADERS)
    assert response.status_code == 400
    assert response.json()["code"] == "slack_missing_scope"


@patch("guildbotics.app_api.api.dotenv_values")
@patch("guildbotics.app_api.avatar.httpx.AsyncClient")
@patch(
    "guildbotics.editions.simple.setup_service.SimplePersonSetupService.read_person_config"
)
def test_import_avatar_from_slack_agent_no_user_id(
    mock_read_config: MagicMock,
    mock_async_client_cls: MagicMock,
    mock_dotenv: MagicMock,
    client: TestClient,
    test_workspace: Path,
) -> None:
    # Mock bot config (no slack_user_id)
    mock_read_config.return_value = PersonConfigSnapshot(
        person_id="bob",
        person_name="Bob",
        person_type="agent",
        is_active=True,
        github_username="",
        git_email="",
    )

    # Create members dir for bob
    (test_workspace / ".guildbotics/config/team/members/bob").mkdir(
        parents=True, exist_ok=True
    )
    (test_workspace / ".env").touch()

    # Bob's bot token
    mock_dotenv.return_value = {"BOB_SLACK_BOT_TOKEN": "xoxb-bob-token"}

    mock_client = MagicMock()
    mock_async_client_cls.return_value.__aenter__.return_value = mock_client

    # auth.test response
    mock_auth_response = MagicMock()
    mock_auth_response.json.return_value = {
        "ok": True,
        "user_id": "U99999",
    }
    mock_auth_response.raise_for_status = MagicMock()

    # users.info response
    mock_user_response = MagicMock()
    mock_user_response.json.return_value = {
        "ok": True,
        "user": {"profile": {"image_512": "https://slack.com/avatar/bob.png"}},
    }
    mock_user_response.raise_for_status = MagicMock()

    # image download response
    mock_image_response = MagicMock()
    mock_image_response.headers = {"Content-Type": "image/png"}
    mock_image_response.content = b"bob image binary"
    mock_image_response.raise_for_status = MagicMock()

    mock_client.post = AsyncMock(return_value=mock_auth_response)
    mock_client.get = AsyncMock(side_effect=[mock_user_response, mock_image_response])

    response = client.post("/config/members/bob/avatar/slack", headers=AUTH_HEADERS)
    assert response.status_code == 200

    # Verify post and get calls
    mock_client.post.assert_called_once_with(
        "https://slack.com/api/auth.test",
        headers={"Authorization": "Bearer xoxb-bob-token"},
        timeout=10.0,
    )
    assert mock_client.get.call_count == 2
    mock_client.get.assert_any_call(
        "https://slack.com/api/users.info",
        params={"user": "U99999"},
        headers={"Authorization": "Bearer xoxb-bob-token"},
        timeout=10.0,
    )

    # Verify image saved
    saved_avatar = test_workspace / ".guildbotics/config/team/members/bob/avatar.png"
    assert saved_avatar.exists()
    assert saved_avatar.read_bytes() == b"bob image binary"


@patch("guildbotics.app_api.api.dotenv_values")
@patch("guildbotics.app_api.avatar.httpx.AsyncClient")
@patch(
    "guildbotics.editions.simple.setup_service.SimplePersonSetupService.read_person_config"
)
def test_import_avatar_from_slack_human_token_fallback(
    mock_read_config: MagicMock,
    mock_async_client_cls: MagicMock,
    mock_dotenv: MagicMock,
    client: TestClient,
    test_workspace: Path,
) -> None:
    # Human member (has user_id, no bot token)
    mock_read_config.return_value = PersonConfigSnapshot(
        person_id="alice",
        person_name="Alice",
        person_type="human",
        is_active=True,
        slack_user_id="U12345",
        github_username="",
        git_email="",
    )

    (test_workspace / ".env").touch()

    # No token for Alice, but has a token for Bob in the workspace
    mock_dotenv.return_value = {"BOB_SLACK_BOT_TOKEN": "xoxb-bob-token"}

    mock_client = MagicMock()
    mock_async_client_cls.return_value.__aenter__.return_value = mock_client

    mock_user_response = MagicMock()
    mock_user_response.json.return_value = {
        "ok": True,
        "user": {"profile": {"image_512": "https://slack.com/avatar/alice.png"}},
    }
    mock_user_response.raise_for_status = MagicMock()

    mock_image_response = MagicMock()
    mock_image_response.headers = {"Content-Type": "image/png"}
    mock_image_response.content = b"alice image binary"
    mock_image_response.raise_for_status = MagicMock()

    mock_client.get = AsyncMock(side_effect=[mock_user_response, mock_image_response])

    response = client.post("/config/members/alice/avatar/slack", headers=AUTH_HEADERS)
    assert response.status_code == 200

    # Verify calls used Bob's token for Alice's user_id
    mock_client.get.assert_any_call(
        "https://slack.com/api/users.info",
        params={"user": "U12345"},
        headers={"Authorization": "Bearer xoxb-bob-token"},
        timeout=10.0,
    )

    # Verify image saved
    saved_avatar = test_workspace / ".guildbotics/config/team/members/alice/avatar.png"
    assert saved_avatar.exists()
    assert saved_avatar.read_bytes() == b"alice image binary"
