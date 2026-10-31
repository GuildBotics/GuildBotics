from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.models import ConfigStatus
from guildbotics.utils.local_api import API_PREFIX
from guildbotics.utils.person_id import identity_members_dir
from guildbotics.utils.secret_store import KeyringSecretStore, identity_secret_store
from tests.guildbotics.app_api.owner import (
    ORIGIN,
    browser_client,
    owner_client,
    owner_token,
)
from tests.guildbotics.app_api.provider_doubles import (
    answer,
    form_of,
    route_web,
    saved_workspace,
    write_member,
)
from tests.guildbotics.integrations.branvil_double import (
    MEMBER,
    BranvilDouble,
    connect_admin,
    serve,
)

PNG = b"\x89PNG\r\n\x1a\nimage"


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

    def stop_scheduler(self, *, force: bool = False) -> None:
        pass

    async def close_cli_agent_usage(self) -> None:
        pass

    def get_config_status(self) -> ConfigStatus:
        return self.config_status

    def saved_team(self) -> None:
        raise AssertionError("reading and uploading an avatar need no team")


@pytest.fixture
def test_workspace(tmp_path: Path) -> Path:
    write_member(tmp_path, "alice")
    return tmp_path


@pytest.fixture
def client(test_workspace: Path) -> TestClient:
    runtime = RuntimeStub(test_workspace)
    return owner_client(create_app(runtime=runtime))


def test_get_avatar_not_found(client: TestClient) -> None:
    response = client.get("/config/members/alice/avatar")
    assert response.status_code == 404
    assert response.json()["code"] == "avatar_not_found"


def test_get_avatar_found(client: TestClient, test_workspace: Path) -> None:
    avatar_file = identity_members_dir() / "alice/avatar.png"
    avatar_file.write_bytes(b"fake image data")

    response = client.get("/config/members/alice/avatar")
    assert response.status_code == 200
    assert response.content == b"fake image data"
    assert response.headers.get("Cache-Control") == "no-cache"


def test_get_avatar_is_served_to_a_browser_session(test_workspace: Path) -> None:
    # An <img> tag sends no header of its own, but it sends the session cookie.
    avatar_file = identity_members_dir() / "alice/avatar.png"
    avatar_file.write_bytes(b"fake image data")
    app = create_app(runtime=RuntimeStub(test_workspace))

    response = browser_client(app).get("/config/members/alice/avatar")
    assert response.status_code == 200
    assert response.content == b"fake image data"


def test_get_avatar_rejects_a_request_without_a_credential(
    test_workspace: Path,
) -> None:
    avatar_file = identity_members_dir() / "alice/avatar.png"
    avatar_file.write_bytes(b"fake image data")
    app = create_app(runtime=RuntimeStub(test_workspace))
    anonymous = TestClient(app, base_url=ORIGIN + API_PREFIX)

    response = anonymous.get("/config/members/alice/avatar")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"

    # The token in the query, which the image used to carry, is no credential.
    response = anonymous.get(
        "/config/members/alice/avatar", params={"token": owner_token(app)}
    )
    assert response.status_code == 401


def test_post_avatar_upload(client: TestClient, test_workspace: Path) -> None:
    file_data = b"uploaded image content"
    files = {"file": ("avatar.png", io.BytesIO(file_data), "image/png")}

    response = client.post("/config/members/alice/avatar", files=files)
    assert response.status_code == 200

    saved_avatar = identity_members_dir() / "alice/avatar.png"
    assert saved_avatar.exists()
    assert saved_avatar.read_bytes() == file_data
    # The response carries the saved file mtime as a deterministic cache-buster.
    assert response.json()["avatar_timestamp"] == int(saved_avatar.stat().st_mtime)


def test_post_avatar_upload_rejects_large_file(client: TestClient) -> None:
    from guildbotics.app_api.avatar import MAX_AVATAR_BYTES

    oversized = b"x" * (MAX_AVATAR_BYTES + 1)
    files = {"file": ("avatar.png", io.BytesIO(oversized), "image/png")}

    response = client.post("/config/members/alice/avatar", files=files)
    assert response.status_code == 400
    assert response.json()["code"] == "avatar_invalid"


# --- handing the avatar to the member's accounts ---------------------------


@pytest.mark.parametrize("source", ["upload", "github"])
def test_a_new_avatar_is_handed_to_the_members_branvil_account(
    workspace, web, monkeypatch, source: str
) -> None:
    double = BranvilDouble()
    serve(double, monkeypatch)
    branvil = {"name": "branvil", "workspace": "a"}
    with saved_workspace(workspace, {"code_hosting_service": branvil}) as client:
        connect_admin()
        write_member(
            workspace,
            "alice",
            f"account_info:\n  branvil_username: {MEMBER}\n"
            "  github_username: alice-git\n",
        )
        web(
            {
                "api.github.com/users/alice-git": answer(
                    {"avatar_url": "https://avatars.example/alice.png"}
                ),
                "avatars.example/alice.png": _image(),
            }
        )
        if source == "upload":
            response = client.post(
                "/config/members/alice/avatar",
                files={"file": ("avatar.png", io.BytesIO(PNG), "image/png")},
            )
        else:
            response = client.post("/config/members/alice/avatar/github")

    assert response.status_code == 200, response.text
    assert double.avatars == {MEMBER: PNG}


# --- importing from a provider -------------------------------------------


@pytest.fixture
def app_client(workspace: Path) -> Iterator[TestClient]:
    with saved_workspace(workspace, {"chat_service": {"name": "slack"}}) as client:
        yield client


def _secret(workspace: Path, key: str, value: str) -> None:
    identity_secret_store().set(key, value)


def _avatar(workspace: Path, person_id: str) -> Path:
    """The member's picture: part of its identity, in no workspace."""
    return identity_members_dir() / person_id / "avatar.png"


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    return lambda routes: route_web(monkeypatch, routes)


def _image(content: bytes = PNG) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(
        200, content=content, headers={"Content-Type": "image/png"}
    )


def test_import_avatar_from_github(app_client, workspace, web) -> None:
    write_member(workspace, "alice", "account_info:\n  github_username: alice-git\n")
    requests = web(
        {
            "api.github.com/users/alice-git": answer(
                {"avatar_url": "https://avatars.example/alice.png"}
            ),
            "avatars.example/alice.png": _image(),
        }
    )

    response = app_client.post("/config/members/alice/avatar/github")

    assert response.status_code == 200
    assert [str(request.url) for request in requests] == [
        "https://api.github.com/users/alice-git",
        "https://avatars.example/alice.png",
    ]
    saved = _avatar(workspace, "alice")
    assert saved.read_bytes() == PNG
    assert response.json()["avatar_timestamp"] == int(saved.stat().st_mtime)


@pytest.mark.parametrize(
    ("language", "message"),
    [
        ("en", "GitHub username is not configured for this member."),
        ("ja", "このメンバーには GitHub ユーザー名が設定されていません。"),
    ],
)
def test_import_avatar_from_github_needs_the_members_username(
    app_client, workspace, web, language: str, message: str
) -> None:
    write_member(workspace, "alice", "account_info:\n  git_email: a@example.com\n")
    requests = web({})

    response = app_client.post(
        "/config/members/alice/avatar/github",
        headers={"X-GuildBotics-Language": language},
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "avatar_account_missing",
        "message": message,
        "context": {},
    }
    assert requests == []


def test_a_provider_failure_is_reported_without_its_detail(
    app_client, workspace, web
) -> None:
    write_member(workspace, "alice", "account_info:\n  github_username: alice-git\n")

    def boom(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("secret internal detail")

    web({"api.github.com/users/alice-git": boom})

    response = app_client.post("/config/members/alice/avatar/github")

    assert response.status_code == 500
    assert response.json() == {
        "code": "avatar_import_failed",
        "message": "Failed to import avatar from GitHub.",
        "context": {},
    }


@pytest.mark.parametrize("provider", ["gitlab", "local"])
def test_import_avatar_from_a_provider_without_avatars_is_not_found(
    app_client, workspace, provider: str
) -> None:
    write_member(workspace, "alice", "account_info:\n  github_username: alice-git\n")

    response = app_client.post(f"/config/members/alice/avatar/{provider}")

    assert response.status_code == 404
    assert response.json()["code"] == "avatar_provider_unknown"
    assert not _avatar(workspace, "alice").exists()


def test_import_avatar_for_an_unknownwrite_member(app_client, workspace) -> None:
    response = app_client.post("/config/members/ghost/avatar/github")

    assert response.status_code == 400
    assert response.json()["code"] == "person_not_found"


def test_import_avatar_from_slack_by_the_members_user_id(
    app_client, workspace, web
) -> None:
    write_member(
        workspace,
        "alice",
        "person_type: agent\naccount_info:\n  slack_user_id: U12345\n",
    )
    _secret(workspace, "ALICE_SLACK_BOT_TOKEN", "xoxb-alice-token")
    requests = web(
        {
            "slack.com/api/users.info": answer(
                {
                    "ok": True,
                    "user": {"profile": {"image_512": "https://slack.example/a.png"}},
                }
            ),
            "slack.example/a.png": _image(),
        }
    )

    response = app_client.post("/config/members/alice/avatar/slack")

    assert response.status_code == 200
    assert requests[0].headers["Authorization"] == "Bearer xoxb-alice-token"
    assert form_of(requests[0]) == {"user": "U12345"}
    assert _avatar(workspace, "alice").read_bytes() == PNG


def test_import_avatar_from_slack_for_a_bot_asks_who_the_token_is(
    app_client, workspace, web
) -> None:
    write_member(workspace, "bob", "person_type: agent\n")
    _secret(workspace, "BOB_SLACK_BOT_TOKEN", "xoxb-bob-token")
    requests = web(
        {
            "slack.com/api/auth.test": answer(
                {"ok": True, "user_id": "U99999", "user": "bob", "team": "acme"}
            ),
            "slack.com/api/users.info": answer(
                {
                    "ok": True,
                    "user": {"profile": {"image_192": "https://slack.example/b.png"}},
                }
            ),
            "slack.example/b.png": _image(),
        }
    )

    response = app_client.post("/config/members/bob/avatar/slack")

    assert response.status_code == 200
    assert [request.url.path for request in requests] == [
        "/api/auth.test",
        "/api/users.info",
        "/b.png",
    ]
    assert form_of(requests[1]) == {"user": "U99999"}
    assert all(
        request.headers["Authorization"] == "Bearer xoxb-bob-token"
        for request in requests[:2]
    )
    assert _avatar(workspace, "bob").read_bytes() == PNG


def test_import_avatar_from_slack_for_a_human_uses_another_members_token(
    app_client, workspace, web
) -> None:
    write_member(
        workspace,
        "alice",
        "person_type: human\naccount_info:\n  slack_user_id: U12345\n",
    )
    write_member(workspace, "bob", "person_type: agent\n")
    _secret(workspace, "BOB_SLACK_BOT_TOKEN", "xoxb-bob-token")
    requests = web(
        {
            "slack.com/api/users.info": answer(
                {
                    "ok": True,
                    "user": {"profile": {"image_512": "https://slack.example/a.png"}},
                }
            ),
            "slack.example/a.png": _image(),
        }
    )

    response = app_client.post("/config/members/alice/avatar/slack")

    assert response.status_code == 200
    assert requests[0].headers["Authorization"] == "Bearer xoxb-bob-token"
    assert form_of(requests[0]) == {"user": "U12345"}


def test_import_avatar_from_slack_env_var_wins_over_keychain(
    app_client, workspace, web, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real environment variables take precedence over OS-keychain values."""
    write_member(
        workspace,
        "alice",
        "person_type: agent\naccount_info:\n  slack_user_id: U12345\n",
    )
    _secret(workspace, "ALICE_SLACK_BOT_TOKEN", "xoxb-keychain-token")
    monkeypatch.setenv("ALICE_SLACK_BOT_TOKEN", "xoxb-env-token")
    requests = web(
        {
            "slack.com/api/users.info": answer(
                {
                    "ok": True,
                    "user": {"profile": {"image_512": "https://slack.example/a.png"}},
                }
            ),
            "slack.example/a.png": _image(),
        }
    )

    response = app_client.post("/config/members/alice/avatar/slack")

    assert response.status_code == 200
    assert requests[0].headers["Authorization"] == "Bearer xoxb-env-token"


def test_import_avatar_from_slack_missing_scope(app_client, workspace, web) -> None:
    write_member(
        workspace,
        "alice",
        "person_type: agent\naccount_info:\n  slack_user_id: U12345\n",
    )
    _secret(workspace, "ALICE_SLACK_BOT_TOKEN", "xoxb-alice-token")
    web(
        {
            "slack.com/api/users.info": answer(
                {"ok": False, "error": "missing_scope", "needed": "users:read"}
            )
        }
    )

    response = app_client.post(
        "/config/members/alice/avatar/slack",
        headers={"X-GuildBotics-Language": "ja"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "avatar_missing_scope"
    assert "users:read" in response.json()["message"]
    assert "再インストール" in response.json()["message"]


def test_import_avatar_from_slack_needs_a_bot_token(
    app_client, workspace, web, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_member(
        workspace,
        "alice",
        "person_type: human\naccount_info:\n  slack_user_id: U12345\n",
    )
    monkeypatch.delenv("ALICE_SLACK_BOT_TOKEN", raising=False)
    requests = web({})

    response = app_client.post("/config/members/alice/avatar/slack")

    assert response.status_code == 400
    assert response.json() == {
        "code": "avatar_token_missing",
        "message": "Slack Bot Token is not configured in this workspace.",
        "context": {},
    }
    assert requests == []


@pytest.mark.parametrize("requested", ["alice-bak", "alice"])
def test_slack_avatar_never_reads_or_uses_colliding_member_token(
    app_client, workspace, web, monkeypatch: pytest.MonkeyPatch, requested: str
) -> None:
    for name in ("alice", "alice-bak", "alice_bak"):
        write_member(
            workspace,
            name,
            "person_type: agent\naccount_info:\n  slack_user_id: U12345\n",
        )
    _secret(workspace, "ALICE_BAK_SLACK_BOT_TOKEN", "fake-keychain-token")
    # Cover both sources, including fallback for a unique requested member.
    monkeypatch.setenv("ALICE_BAK_SLACK_BOT_TOKEN", "fake-env-token")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALICE_SLACK_BOT_TOKEN", raising=False)
    original = KeyringSecretStore.get

    def guarded_get(self: KeyringSecretStore, key: str) -> str | None:
        assert key != "ALICE_BAK_SLACK_BOT_TOKEN"
        return original(self, key)

    monkeypatch.setattr(KeyringSecretStore, "get", guarded_get)
    requests = web({})

    response = app_client.post(f"/config/members/{requested}/avatar/slack")

    # The saved team cannot be read while two members share a secret prefix,
    # so no member's avatar is imported until the collision is repaired.
    assert response.status_code == 400
    assert response.json()["code"] == "member_config_invalid"
    assert requests == []


def test_an_imported_avatar_is_refused_when_it_is_too_large(
    app_client, workspace, web
) -> None:
    """A provider URL is the other way an avatar arrives, so it needs the same
    limit: checking only the upload still lets in one that cannot be shared."""
    from guildbotics.app_api.avatar import MAX_AVATAR_BYTES

    write_member(workspace, "alice", "account_info:\n  github_username: alice-git\n")
    web(
        {
            "api.github.com/users/alice-git": answer(
                {"avatar_url": "https://avatars.example/alice.png"}
            ),
            "avatars.example/alice.png": _image(b"x" * (MAX_AVATAR_BYTES + 1)),
        }
    )

    response = app_client.post("/config/members/alice/avatar/github")

    assert response.status_code != 200
    assert not _avatar(workspace, "alice").exists()
