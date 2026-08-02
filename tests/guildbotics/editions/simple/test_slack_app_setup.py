import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from guildbotics.editions.simple import slack_app_setup
from guildbotics.editions.simple.setup_service import SetupServiceError
from guildbotics.integrations.slack import app_manifest, slack_chat_service

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


def test_start_registration_returns_deep_link():
    info = slack_app_setup.start_registration("  Alice Bot  ")

    assert info.app_name == "Alice Bot"
    assert info.app_directory_url == "https://api.slack.com/apps"
    query = parse_qs(urlparse(info.registration_url).query)
    assert json.loads(query["manifest_json"][0]) == app_manifest.build_app_manifest(
        "Alice Bot"
    )


@pytest.mark.parametrize("app_name", ["", "   ", "a" * 36])
def test_start_registration_rejects_invalid_app_name(app_name):
    with pytest.raises(SetupServiceError) as excinfo:
        slack_app_setup.start_registration(app_name)

    assert excinfo.value.code == "invalid_slack_app_name"


def test_start_registration_accepts_the_maximum_length_name():
    name = "a" * app_manifest.APP_NAME_MAX_LENGTH

    assert slack_app_setup.start_registration(name).app_name == name


@pytest.mark.asyncio
async def test_verify_tokens_reports_bot_identity_and_workspace():
    result = await slack_app_setup.verify_tokens(
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

    await slack_app_setup.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(handler)
    )

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

    result = await slack_app_setup.verify_tokens(
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

    result = await slack_app_setup.verify_tokens(
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

    result = await slack_app_setup.verify_tokens(
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

    result = await slack_app_setup.verify_tokens(
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

    result = await slack_app_setup.verify_tokens(
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

    result = await slack_app_setup.verify_tokens(
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

    result = await slack_app_setup.verify_tokens(
        BOT_TOKEN, APP_TOKEN, channels=["general"], transport=_transport(handler)
    )

    assert [(c.channel, c.ok) for c in result.channels] == [("general", True)]


@pytest.mark.asyncio
async def test_verify_tokens_reports_an_unresolvable_channel_name():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conversations.list"):
            return httpx.Response(200, json={"ok": True, "channels": []})
        return _slack_ok(request)

    result = await slack_app_setup.verify_tokens(
        BOT_TOKEN, APP_TOKEN, channels=["nope"], transport=_transport(handler)
    )

    assert result.channels[0].error == slack_app_setup.ERROR_CHANNEL_NOT_FOUND


@pytest.mark.asyncio
async def test_verify_tokens_uses_a_channel_id_without_resolving_it():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/conversations.history"):
            return httpx.Response(200, json={"ok": True, "messages": []})
        return _slack_ok(request)

    result = await slack_app_setup.verify_tokens(
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

    result = await slack_app_setup.verify_tokens(
        BOT_TOKEN, APP_TOKEN, channels=["general"], transport=_transport(handler)
    )

    assert result.scopes_ok is False
    assert result.channels == []


@pytest.mark.asyncio
async def test_verify_tokens_reports_the_source_of_each_checked_token():
    result = await slack_app_setup.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(_slack_ok)
    )

    assert result.bot_source == slack_app_setup.SOURCE_INPUT
    assert result.app_token_source == slack_app_setup.SOURCE_INPUT


@pytest.mark.asyncio
async def test_verify_tokens_falls_back_to_the_stored_token_for_empty_fields():
    """An empty field means "keep the stored token" when the member is saved."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.headers["Authorization"]
        return _slack_ok(request)

    result = await slack_app_setup.verify_tokens(
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
    assert result.bot_source == slack_app_setup.SOURCE_STORED
    assert result.app_token_source == slack_app_setup.SOURCE_STORED


@pytest.mark.asyncio
async def test_verify_tokens_prefers_the_entered_token_over_the_stored_one():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return _slack_ok(request)

    result = await slack_app_setup.verify_tokens(
        BOT_TOKEN,
        "",
        stored_bot_token="xoxb-stale",
        stored_app_token=APP_TOKEN,
        transport=_transport(handler),
    )

    assert f"Bearer {BOT_TOKEN}" in seen
    assert "Bearer xoxb-stale" not in seen
    assert result.bot_source == slack_app_setup.SOURCE_INPUT
    assert result.app_token_source == slack_app_setup.SOURCE_STORED


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

    result = await slack_app_setup.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(handler)
    )

    assert result.bot_error == "invalid_auth"
    assert recorded == []


@pytest.mark.asyncio
async def test_verify_tokens_reports_missing_tokens_without_calling_slack():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("Slack must not be called for empty tokens")

    result = await slack_app_setup.verify_tokens(
        "  ", "", transport=_transport(handler)
    )

    assert result.bot_error == slack_app_setup.ERROR_MISSING
    assert result.app_token_error == slack_app_setup.ERROR_MISSING
    assert result.bot_ok is False
    assert result.app_token_ok is False


@pytest.mark.asyncio
async def test_verify_tokens_detects_swapped_bot_and_app_tokens():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("Slack must not be called for a wrong token type")

    result = await slack_app_setup.verify_tokens(
        APP_TOKEN, BOT_TOKEN, transport=_transport(handler)
    )

    assert result.bot_error == slack_app_setup.ERROR_WRONG_TOKEN_TYPE
    assert result.app_token_error == slack_app_setup.ERROR_WRONG_TOKEN_TYPE


@pytest.mark.asyncio
async def test_verify_tokens_reports_network_failures_without_leaking_the_token():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to connect with {BOT_TOKEN}")

    result = await slack_app_setup.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(handler)
    )

    assert result.bot_error == slack_app_setup.ERROR_UNREACHABLE
    assert result.app_token_error == slack_app_setup.ERROR_UNREACHABLE
    assert BOT_TOKEN not in result.model_dump_json()
    assert APP_TOKEN not in result.model_dump_json()


@pytest.mark.asyncio
async def test_verify_tokens_reports_http_errors_as_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    result = await slack_app_setup.verify_tokens(
        BOT_TOKEN, APP_TOKEN, transport=_transport(handler)
    )

    assert result.bot_error == slack_app_setup.ERROR_UNREACHABLE
    assert result.app_token_error == slack_app_setup.ERROR_UNREACHABLE
