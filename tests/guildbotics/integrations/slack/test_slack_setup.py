"""Slack's setup: the chat's items, a member's Slack account, the App
registration link, and the check of the tokens a member is given."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import ValidationError

from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.provider import SetupError
from guildbotics.integrations.slack import app_manifest, slack_chat_service
from guildbotics.integrations.slack import setup as slack_app
from guildbotics.integrations.slack.setup import (
    AccountView,
    AppRegistrationStart,
    Chat,
    SlackAccount,
    TokenCheck,
)

BOT_TOKEN = "xoxb-valid"
APP_TOKEN = "xapp-valid"


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _slack_ok(request: httpx.Request) -> httpx.Response:
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


def _no_team() -> Team:
    raise AssertionError("the operation does not read the saved team")


def _start(app_name: str):
    return slack_app.start_registration(
        AppRegistrationStart(app_name=app_name), _no_team
    )


# --------------------------------------------------------------------------- #
# the chat's items and a member's account
# --------------------------------------------------------------------------- #


def test_the_chat_names_its_web_api_only_when_it_is_not_slacks() -> None:
    assert Chat().model_dump(exclude_none=True) == {}
    assert Chat(base_url="https://proxy.example/api").base_url == (
        "https://proxy.example/api"
    )
    with pytest.raises(ValidationError):
        Chat(token="x")


def test_a_human_stores_only_a_user_id() -> None:
    account = SlackAccount(user_id="U012345678", bot_token="xoxb-x", app_token="xapp")

    assert account.stored("alice", True) == ({"slack_user_id": "U012345678"}, {})
    assert SlackAccount().stored("alice", True) == ({}, {})


def test_an_agent_stores_only_the_tokens_it_is_given() -> None:
    assert SlackAccount(
        user_id="U012345678", bot_token="xoxb-x", app_token="xapp-x"
    ).stored("alice", False) == (
        {},
        {"SLACK_BOT_TOKEN": "xoxb-x", "SLACK_APP_TOKEN": "xapp-x"},
    )
    # A blank token keeps the stored one: the save writes nothing for it.
    assert SlackAccount(bot_token="xoxb-x").stored("alice", False) == (
        {},
        {"SLACK_BOT_TOKEN": "xoxb-x"},
    )


def test_the_view_shows_the_user_id_and_which_tokens_are_held() -> None:
    held = {"SLACK_APP_TOKEN"}

    view = SlackAccount.view({"slack_user_id": "U012345678"}, held.__contains__)

    assert view == AccountView(
        user_id="U012345678", has_bot_token=False, has_app_token=True
    )


@pytest.mark.parametrize(
    "reference, chat",
    [
        ("C0123456789", {"channel_id": "C0123456789"}),
        ("general", {"channel_name": "general"}),
        ("c0123456789", {"channel_name": "c0123456789"}),
    ],
)
def test_a_channel_is_named_by_its_id_or_its_name(reference: str, chat: dict) -> None:
    assert slack_app.SETUP.chat_channel is not None
    assert slack_app.SETUP.chat_channel(reference) == chat


# --------------------------------------------------------------------------- #
# the App registration link
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_start_registration_returns_deep_link():
    info = await _start("  Alice Bot  ")

    assert info.app_name == "Alice Bot"
    assert info.app_directory_url == "https://api.slack.com/apps"
    query = parse_qs(urlparse(info.registration_url).query)
    assert json.loads(query["manifest_json"][0]) == app_manifest.build_app_manifest(
        "Alice Bot"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("app_name", ["   ", "a" * 36])
async def test_start_registration_rejects_invalid_app_name(app_name):
    with pytest.raises(SetupError) as excinfo:
        await _start(app_name)

    assert excinfo.value.code == "invalid_slack_app_name"


def test_start_registration_requires_a_name():
    with pytest.raises(ValidationError):
        AppRegistrationStart(app_name="")


@pytest.mark.asyncio
async def test_start_registration_accepts_the_maximum_length_name():
    name = "a" * app_manifest.APP_NAME_MAX_LENGTH

    assert (await _start(name)).app_name == name


# --------------------------------------------------------------------------- #
# the verify-tokens operation: what a save would leave in effect
# --------------------------------------------------------------------------- #


@pytest.fixture
def captured_verify(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    async def verify(bot_token, app_token, **kwargs):
        calls.append({"bot_token": bot_token, "app_token": app_token, **kwargs})
        return slack_app.SlackTokenVerification()

    monkeypatch.setattr(slack_app, "verify_tokens", verify)
    return calls


@pytest.mark.asyncio
async def test_the_check_falls_back_to_the_saved_members_tokens(
    captured_verify: list[dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_SLACK_BOT_TOKEN", "xoxb-stored")
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "xapp-stored")
    team = Team(
        project=Project(
            name="test",
            services={
                "chat_service": {"name": "slack", "base_url": "https://proxy.example"}
            },
        ),
        members=[Person(person_id="alice", name="Alice")],
    )

    await slack_app.SETUP.operations["verify-tokens"].run(
        TokenCheck(bot_token="xoxb-new", person_id="alice", channels=["general"]),
        lambda: team,
    )

    assert captured_verify == [
        {
            "bot_token": "xoxb-new",
            "app_token": "",
            "stored_bot_token": "xoxb-stored",
            "stored_app_token": "xapp-stored",
            "channels": ["general"],
            "base_url": "https://proxy.example",
        }
    ]


@pytest.mark.asyncio
async def test_the_check_of_a_new_member_has_no_stored_tokens(
    captured_verify: list[dict],
) -> None:
    def unreadable() -> Team:
        raise OSError("no saved team")

    run = slack_app.SETUP.operations["verify-tokens"].run
    await run(TokenCheck(bot_token="xoxb-new"), _no_team)
    await run(TokenCheck(bot_token="xoxb-new", person_id="alice"), unreadable)

    for call in captured_verify:
        assert call["stored_bot_token"] == call["stored_app_token"] == ""
        assert call["base_url"] is None


# --------------------------------------------------------------------------- #
# verify_tokens
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_verify_tokens_reports_bot_identity_and_workspace():
    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(_slack_ok)
    )

    assert result.bot_ok is True
    assert result.bot_user_id == "U0BOT"
    assert result.bot_display_name == "alice-bot"
    assert result.workspace == "GuildBotics HQ"
    assert result.bot_error == ""
    assert result.app_token_ok is True
    assert result.app_token_error == ""


@pytest.mark.asyncio
async def test_verify_tokens_sends_each_token_to_its_own_slack_method():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.headers["Authorization"]
        return _slack_ok(request)

    await slack_app.verify_tokens(BOT_TOKEN, APP_TOKEN, transport=_transport(handler))

    assert seen == {
        "/api/auth.test": f"Bearer {BOT_TOKEN}",
        "/api/conversations.list": f"Bearer {BOT_TOKEN}",
        "/api/apps.connections.open": f"Bearer {APP_TOKEN}",
    }


@pytest.mark.asyncio
async def test_verify_tokens_surfaces_slack_error_codes_per_token():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth.test"):
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        return httpx.Response(
            200, json={"ok": False, "error": "not_allowed_token_type"}
        )

    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(handler)
    )

    assert result.bot_ok is False
    assert result.bot_error == "invalid_auth"
    assert result.app_token_ok is False
    assert result.app_token_error == "not_allowed_token_type"


@pytest.mark.asyncio
async def test_verify_tokens_keeps_a_valid_token_when_the_other_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth.test"):
            return _slack_ok(request)
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(handler)
    )

    assert result.bot_ok is True
    assert result.bot_user_id == "U0BOT"
    assert result.app_token_ok is False
    assert result.app_token_error == "invalid_auth"


@pytest.mark.asyncio
async def test_verify_tokens_flags_a_token_whose_scopes_were_never_granted():
    """``auth.test`` needs no scope, so authentication alone proves nothing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conversations.list"):
            return httpx.Response(
                200,
                json={
                    "ok": False,
                    "error": "missing_scope",
                    "needed": "channels:read",
                    "provided": "chat:write",
                },
            )
        return _slack_ok(request)

    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(handler)
    )

    assert result.bot_ok is True
    assert result.scopes_ok is False
    assert result.scope_error == "missing_scope"
    assert result.scope_needed == "channels:read"


@pytest.mark.asyncio
async def test_verify_tokens_confirms_scopes_with_a_narrow_read():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conversations.list"):
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"ok": True, "channels": []})
        return _slack_ok(request)

    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(handler)
    )

    assert result.scopes_ok is True
    assert result.scope_error == ""
    # One conversation is enough to prove the scope; do not page the workspace.
    assert "limit=1" in seen["body"]


@pytest.mark.asyncio
async def test_verify_tokens_skips_the_scope_probe_when_the_token_is_bad():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conversations.list"):  # pragma: no cover
            raise AssertionError("no point probing scopes on a rejected token")
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(handler)
    )

    assert result.bot_ok is False
    assert result.scopes_ok is False


@pytest.mark.asyncio
async def test_verify_tokens_reports_a_channel_the_bot_never_joined():
    """The most common reason valid tokens still do not work."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conversations.list"):
            return httpx.Response(
                200, json={"ok": True, "channels": [{"id": "C001", "name": "general"}]}
            )
        if request.url.path.endswith("/conversations.history"):
            return httpx.Response(200, json={"ok": False, "error": "not_in_channel"})
        return _slack_ok(request)

    result = await slack_app.verify_tokens(
        BOT_TOKEN,
        APP_TOKEN,
        channels=["#general"],
        transport=_transport(handler),
    )

    assert [(c.channel, c.ok, c.error) for c in result.channels] == [
        ("#general", False, "not_in_channel")
    ]


@pytest.mark.asyncio
async def test_verify_tokens_confirms_a_channel_the_bot_can_read():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conversations.list"):
            return httpx.Response(
                200, json={"ok": True, "channels": [{"id": "C001", "name": "general"}]}
            )
        if request.url.path.endswith("/conversations.history"):
            return httpx.Response(200, json={"ok": True, "messages": []})
        return _slack_ok(request)

    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, channels=["general"], transport=_transport(handler)
    )

    assert [(c.channel, c.ok) for c in result.channels] == [("general", True)]


@pytest.mark.asyncio
async def test_verify_tokens_reports_an_unresolvable_channel_name():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conversations.list"):
            return httpx.Response(200, json={"ok": True, "channels": []})
        return _slack_ok(request)

    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, channels=["nope"], transport=_transport(handler)
    )

    assert result.channels[0].error == slack_app.ERROR_CHANNEL_NOT_FOUND


@pytest.mark.asyncio
async def test_verify_tokens_uses_a_channel_id_without_resolving_it():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/conversations.history"):
            return httpx.Response(200, json={"ok": True, "messages": []})
        return _slack_ok(request)

    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, channels=["C0123456789"], transport=_transport(handler)
    )

    assert result.channels[0].ok is True
    # Only the scope probe lists conversations; an ID needs no name lookup.
    assert seen.count("/api/conversations.list") == 1


@pytest.mark.asyncio
async def test_verify_tokens_skips_channels_when_the_scopes_are_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conversations.history"):  # pragma: no cover
            raise AssertionError("channel checks need the read scopes first")
        if request.url.path.endswith("/conversations.list"):
            return httpx.Response(200, json={"ok": False, "error": "missing_scope"})
        return _slack_ok(request)

    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, channels=["general"], transport=_transport(handler)
    )

    assert result.scopes_ok is False
    assert result.channels == []


@pytest.mark.asyncio
async def test_verify_tokens_reports_the_source_of_each_checked_token():
    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(_slack_ok)
    )

    assert result.bot_source == slack_app.SOURCE_INPUT
    assert result.app_token_source == slack_app.SOURCE_INPUT


@pytest.mark.asyncio
async def test_verify_tokens_falls_back_to_the_stored_token_for_empty_fields():
    """An empty field means "keep the stored token" when the member is saved."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.headers["Authorization"]
        return _slack_ok(request)

    result = await slack_app.verify_tokens(
        "",
        "",
        stored_bot_token=BOT_TOKEN,
        stored_app_token=APP_TOKEN,
        transport=_transport(handler),
    )

    assert seen == {
        "/api/auth.test": f"Bearer {BOT_TOKEN}",
        "/api/conversations.list": f"Bearer {BOT_TOKEN}",
        "/api/apps.connections.open": f"Bearer {APP_TOKEN}",
    }
    assert result.bot_ok is True
    assert result.app_token_ok is True
    assert result.bot_source == slack_app.SOURCE_STORED
    assert result.app_token_source == slack_app.SOURCE_STORED


@pytest.mark.asyncio
async def test_verify_tokens_prefers_the_entered_token_over_the_stored_one():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return _slack_ok(request)

    result = await slack_app.verify_tokens(
        BOT_TOKEN,
        "",
        stored_bot_token="xoxb-stale",
        stored_app_token=APP_TOKEN,
        transport=_transport(handler),
    )

    assert f"Bearer {BOT_TOKEN}" in seen
    assert "Bearer xoxb-stale" not in seen
    assert result.bot_source == slack_app.SOURCE_INPUT
    assert result.app_token_source == slack_app.SOURCE_STORED


@pytest.mark.asyncio
async def test_verify_tokens_does_not_record_a_credential_failure_event(
    monkeypatch: pytest.MonkeyPatch,
):
    """A typo in the setup field must not raise a desktop-wide alert."""
    recorded: list[dict] = []
    monkeypatch.setattr(
        slack_chat_service,
        "record_correlated_event",
        lambda **kwargs: recorded.append(kwargs),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(handler)
    )

    assert result.bot_error == "invalid_auth"
    assert recorded == []


@pytest.mark.asyncio
async def test_verify_tokens_reports_missing_tokens_without_calling_slack():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("Slack must not be called for empty tokens")

    result = await slack_app.verify_tokens("  ", "", transport=_transport(handler))

    assert result.bot_error == slack_app.ERROR_MISSING
    assert result.app_token_error == slack_app.ERROR_MISSING
    assert result.bot_ok is False
    assert result.app_token_ok is False


@pytest.mark.asyncio
async def test_verify_tokens_detects_swapped_bot_and_app_tokens():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("Slack must not be called for a wrong token type")

    result = await slack_app.verify_tokens(
        APP_TOKEN, BOT_TOKEN, transport=_transport(handler)
    )

    assert result.bot_error == slack_app.ERROR_WRONG_TOKEN_TYPE
    assert result.app_token_error == slack_app.ERROR_WRONG_TOKEN_TYPE


@pytest.mark.asyncio
async def test_verify_tokens_reports_network_failures_without_leaking_the_token():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to connect with {BOT_TOKEN}")

    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(handler)
    )

    assert result.bot_error == slack_app.ERROR_UNREACHABLE
    assert result.app_token_error == slack_app.ERROR_UNREACHABLE
    assert BOT_TOKEN not in result.model_dump_json()
    assert APP_TOKEN not in result.model_dump_json()


@pytest.mark.asyncio
async def test_verify_tokens_reports_http_errors_as_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    result = await slack_app.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(handler)
    )

    assert result.bot_error == slack_app.ERROR_UNREACHABLE
    assert result.app_token_error == slack_app.ERROR_UNREACHABLE
