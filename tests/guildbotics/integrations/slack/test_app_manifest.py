import json
from urllib.parse import parse_qs, urlparse

import pytest

from guildbotics.integrations.slack import app_manifest


def test_build_app_manifest_contains_required_scopes_and_events():
    manifest = app_manifest.build_app_manifest("alice")

    assert manifest["display_information"]["name"] == "alice"
    assert manifest["oauth_config"]["scopes"]["bot"] == [
        "chat:write",
        "reactions:write",
        "channels:history",
        "groups:history",
        "im:history",
        "mpim:history",
        "channels:read",
        "groups:read",
        "users:read",
    ]
    settings = manifest["settings"]
    assert settings["socket_mode_enabled"] is True
    assert settings["event_subscriptions"]["bot_events"] == [
        "message.channels",
        "message.groups",
        "message.im",
        "message.mpim",
    ]
    assert settings["org_deploy_enabled"] is False
    assert settings["token_rotation_enabled"] is False


def test_build_app_manifest_keeps_bot_user_always_online():
    manifest = app_manifest.build_app_manifest("alice")

    assert manifest["features"]["bot_user"] == {
        "display_name": "alice",
        "always_online": True,
    }


def test_build_app_manifest_does_not_share_module_level_lists():
    first = app_manifest.build_app_manifest("alice")
    first["oauth_config"]["scopes"]["bot"].append("admin")

    second = app_manifest.build_app_manifest("bob")

    assert "admin" not in second["oauth_config"]["scopes"]["bot"]


@pytest.mark.parametrize(
    ("app_name", "expected"),
    [
        ("alice", "alice"),
        ("Alice Bot", "alice-bot"),
        ("guild.botics_1", "guild.botics_1"),
        ("  Team  Assistant  ", "team-assistant"),
        ("エージェント", "guildbotics"),
        ("---", "guildbotics"),
        ("a" * 100, "a" * app_manifest.BOT_DISPLAY_NAME_MAX_LENGTH),
    ],
)
def test_bot_display_name_folds_into_slack_allowed_characters(app_name, expected):
    assert app_manifest.bot_display_name(app_name) == expected


def test_registration_url_carries_url_encoded_manifest_json():
    url = app_manifest.registration_url("Alice Bot")

    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        app_manifest.SLACK_APPS_URL
    )
    query = parse_qs(parsed.query)
    assert query["new_app"] == ["1"]
    # Raw JSON punctuation must not leak into the query string unencoded.
    assert '"' not in parsed.query and "{" not in parsed.query
    assert json.loads(query["manifest_json"][0]) == app_manifest.build_app_manifest(
        "Alice Bot"
    )


def test_registration_url_stays_well_within_browser_url_limits():
    url = app_manifest.registration_url("a" * app_manifest.APP_NAME_MAX_LENGTH)

    assert len(url) < 2000


def test_app_directory_url_points_at_the_app_list():
    assert app_manifest.app_directory_url() == "https://api.slack.com/apps"
