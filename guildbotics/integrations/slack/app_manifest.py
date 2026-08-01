"""Slack App manifest and the deep link that pre-fills app creation.

Slack has no counterpart to GitHub's manifest-code conversion: the OAuth
redirect rejects ``http://localhost`` and app-level tokens have no creation
API, so credentials cannot be collected by a callback. What Slack does offer
is a deep link that opens the app creation dialog with a manifest already
applied, which is what this module builds. The user then copies the two
tokens out of the Slack UI by hand.

This module holds the provider knowledge of that link: the scopes and bot
events GuildBotics needs, the manifest payload, and the URL format.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

SLACK_APPS_URL = "https://api.slack.com/apps"

# ``display_information.name`` accepts at most 35 characters.
APP_NAME_MAX_LENGTH = 35
# ``features.bot_user.display_name`` accepts at most 80 characters and only
# ``a-z``, ``0-9``, ``-``, ``_`` and ``.``.
BOT_DISPLAY_NAME_MAX_LENGTH = 80
_BOT_DISPLAY_NAME_DISALLOWED = re.compile(r"[^a-z0-9._-]+")
_FALLBACK_BOT_DISPLAY_NAME = "guildbotics"
# Channel, private channel and DM IDs; anything else is a channel name.
_CHANNEL_ID = re.compile(r"[CGD][A-Z0-9]{8,}")

# Scopes required by the chat capability (see docs/slack_integration): posting
# and reacting, reading history for every conversation type, resolving channel
# names, and reading user profiles for the avatar import.
BOT_SCOPES: tuple[str, ...] = (
    "chat:write",
    "reactions:write",
    "channels:history",
    "groups:history",
    "im:history",
    "mpim:history",
    "channels:read",
    "groups:read",
    "users:read",
)

# Message events the Socket Mode listener subscribes to.
BOT_EVENTS: tuple[str, ...] = (
    "message.channels",
    "message.groups",
    "message.im",
    "message.mpim",
)


def is_channel_id(reference: str) -> bool:
    """Return whether a channel reference is already a Slack channel ID."""
    return bool(_CHANNEL_ID.fullmatch(reference.strip()))


def bot_display_name(app_name: str) -> str:
    """Convert an app name into a valid ``bot_user.display_name``.

    Slack restricts the bot display name to lowercase alphanumerics and
    ``-``, ``_``, ``.``, so a human-readable app name (spaces, capitals) has
    to be folded before it can go into the manifest.
    """
    folded = _BOT_DISPLAY_NAME_DISALLOWED.sub("-", app_name.strip().lower())
    folded = folded.strip("-")[:BOT_DISPLAY_NAME_MAX_LENGTH]
    return folded or _FALLBACK_BOT_DISPLAY_NAME


def build_app_manifest(app_name: str) -> dict:
    """Build the manifest that pre-configures a GuildBotics agent app.

    Every scope is requested up front: adding one later forces the user to
    reinstall the app, so a full configuration is the friendlier default.
    """
    return {
        "display_information": {"name": app_name},
        "features": {
            "bot_user": {
                "display_name": bot_display_name(app_name),
                "always_online": True,
            }
        },
        "oauth_config": {"scopes": {"bot": list(BOT_SCOPES)}},
        "settings": {
            "socket_mode_enabled": True,
            "event_subscriptions": {"bot_events": list(BOT_EVENTS)},
            "org_deploy_enabled": False,
            "token_rotation_enabled": False,
        },
    }


def registration_url(app_name: str) -> str:
    """Return the deep link that opens app creation with the manifest applied."""
    manifest = json.dumps(build_app_manifest(app_name), separators=(",", ":"))
    return f"{SLACK_APPS_URL}?new_app=1&manifest_json={quote(manifest, safe='')}"


def app_directory_url() -> str:
    """Return the app list page, used to reach an already created app."""
    return SLACK_APPS_URL
