"""Slack App semi-automatic registration used by the desktop member setup GUI.

Unlike the GitHub App flow there is no callback and therefore no in-flight
state: the deep link is built, the user creates the app in their browser, and
comes back with two tokens to paste. What remains for this module is building
that link and verifying the pasted tokens before the member is saved.

Verification failures are returned rather than raised so the GUI can show a
partial result (a valid bot token next to a bad app token, for example).
"""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel

from guildbotics.capabilities.member_chat import probe_slack_app_token
from guildbotics.capabilities.member_github import MemberCapabilityError
from guildbotics.editions.simple.setup_service import SetupServiceError
from guildbotics.integrations.slack import app_manifest
from guildbotics.integrations.slack.slack_chat_service import (
    SlackApiError,
    SlackChatService,
)

BOT_TOKEN_PREFIX = "xoxb-"
APP_TOKEN_PREFIX = "xapp-"

# Result codes shared with the GUI, which maps them to localized messages.
ERROR_MISSING = "missing"
ERROR_WRONG_TOKEN_TYPE = "wrong_token_type"
ERROR_UNREACHABLE = "unreachable"
ERROR_CHANNEL_NOT_FOUND = "not_found"

# Where the verified token came from, so the GUI never reports a token as
# valid without saying which one it actually checked.
SOURCE_INPUT = "input"
SOURCE_STORED = "stored"
SOURCE_NONE = "none"

logger = logging.getLogger(__name__)


class SlackAppRegistrationInfo(BaseModel):
    """The links the desktop opens to create and then reach a Slack App."""

    app_name: str
    registration_url: str
    # Slack gives no app_id back (there is no callback), so the app list page
    # is the closest the GUI can get to the created app's settings.
    app_directory_url: str


class SlackChannelVerification(BaseModel):
    """Whether the bot can actually read one of the member's channels."""

    channel: str
    ok: bool = False
    # Slack error code, e.g. ``not_in_channel``; ``not_found`` when the channel
    # name could not be resolved at all.
    error: str = ""


class SlackTokenVerification(BaseModel):
    """Outcome of probing a bot token and app-level token."""

    bot_ok: bool = False
    bot_user_id: str = ""
    bot_display_name: str = ""
    workspace: str = ""
    bot_error: str = ""
    bot_source: str = SOURCE_NONE
    # Authentication and authorization are separate facts: a token can pass
    # auth.test (which needs no scope) while carrying none of the scopes the
    # app was configured with, because scopes added after an install only
    # reach the token on reinstall.
    scopes_ok: bool = False
    scope_error: str = ""
    scope_needed: str = ""
    app_token_ok: bool = False
    app_token_error: str = ""
    app_token_source: str = SOURCE_NONE
    channels: list[SlackChannelVerification] = []


def start_registration(app_name: str) -> SlackAppRegistrationInfo:
    """Validate the app name and build the manifest deep link."""
    name = app_name.strip()
    if not name or len(name) > app_manifest.APP_NAME_MAX_LENGTH:
        raise SetupServiceError(
            "invalid_slack_app_name",
            f"Slack App name must be 1-{app_manifest.APP_NAME_MAX_LENGTH} characters.",
        )
    return SlackAppRegistrationInfo(
        app_name=name,
        registration_url=app_manifest.registration_url(name),
        app_directory_url=app_manifest.app_directory_url(),
    )


async def verify_tokens(
    bot_token: str,
    app_token: str,
    *,
    stored_bot_token: str = "",
    stored_app_token: str = "",
    channels: list[str] | None = None,
    base_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SlackTokenVerification:
    """Probe what saving the member right now would leave in effect.

    An empty token field means "keep what is stored" on save, so verification
    has to read it the same way instead of calling it missing. The channels
    are checked too: a bot that was never invited is the most common reason a
    fully valid pair of tokens still does not work.
    """
    result = SlackTokenVerification()
    bot, result.bot_source = _effective_token(bot_token, stored_bot_token)
    app, result.app_token_source = _effective_token(app_token, stored_app_token)
    await _verify_bot_token(result, bot, base_url, transport)
    await _verify_app_token(result, app, base_url, transport)
    if result.scopes_ok:
        result.channels = await _verify_channels(
            bot, channels or [], base_url, transport
        )
    return result


async def _verify_channels(
    token: str,
    channels: list[str],
    base_url: str | None,
    transport: httpx.AsyncBaseTransport | None,
) -> list[SlackChannelVerification]:
    """Read one message from each channel, which only a joined bot may do."""
    names = [channel.strip() for channel in channels if channel.strip()]
    if not names:
        return []
    service = SlackChatService(
        logger,
        token=token,
        base_url=base_url,
        transport=transport,
        record_credential_events=False,
    )
    results: list[SlackChannelVerification] = []
    try:
        for name in names:
            results.append(await _verify_channel(service, name))
    finally:
        await service.aclose()
    return results


async def _verify_channel(
    service: SlackChatService, channel: str
) -> SlackChannelVerification:
    reference = channel.lstrip("#")
    try:
        channel_id = (
            reference
            if app_manifest.is_channel_id(reference)
            else await service.resolve_channel_id(reference) or ""
        )
        if not channel_id:
            return SlackChannelVerification(
                channel=channel, error=ERROR_CHANNEL_NOT_FOUND
            )
        await service.list_channel_events(channel_id, limit=1)
    except SlackApiError as exc:
        return SlackChannelVerification(channel=channel, error=exc.error)
    except (httpx.HTTPError, ValueError) as exc:
        return SlackChannelVerification(channel=channel, error=_unreachable_error(exc))
    return SlackChannelVerification(channel=channel, ok=True)


def _effective_token(entered: str, stored: str) -> tuple[str, str]:
    """Return the token that would be in effect, and where it came from."""
    if entered.strip():
        return entered.strip(), SOURCE_INPUT
    if stored.strip():
        return stored.strip(), SOURCE_STORED
    return "", SOURCE_NONE


async def _verify_bot_token(
    result: SlackTokenVerification,
    token: str,
    base_url: str | None,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    error = _prefix_error(token, BOT_TOKEN_PREFIX)
    if error:
        result.bot_error = error
        return
    service = SlackChatService(
        logger,
        token=token,
        base_url=base_url,
        transport=transport,
        # A rejection here answers the user's "is this token good?" on screen;
        # it must not raise a desktop-wide credential alert.
        record_credential_events=False,
    )
    try:
        identity = await service.get_bot_identity()
    except SlackApiError as exc:
        result.bot_error = exc.error
        return
    except (httpx.HTTPError, ValueError) as exc:
        result.bot_error = _unreachable_error(exc)
        return
    finally:
        await service.aclose()
    result.bot_ok = True
    result.bot_user_id = identity.user_id
    result.bot_display_name = identity.display_name
    result.workspace = identity.workspace
    await _verify_bot_scopes(result, token, base_url, transport)


async def _verify_bot_scopes(
    result: SlackTokenVerification,
    token: str,
    base_url: str | None,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    service = SlackChatService(
        logger,
        token=token,
        base_url=base_url,
        transport=transport,
        record_credential_events=False,
    )
    try:
        await service.probe_read_scopes()
    except SlackApiError as exc:
        result.scope_error = exc.error
        result.scope_needed = exc.needed
        return
    except (httpx.HTTPError, ValueError) as exc:
        result.scope_error = _unreachable_error(exc)
        return
    finally:
        await service.aclose()
    result.scopes_ok = True


async def _verify_app_token(
    result: SlackTokenVerification,
    token: str,
    base_url: str | None,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    error = _prefix_error(token, APP_TOKEN_PREFIX)
    if error:
        result.app_token_error = error
        return
    try:
        await probe_slack_app_token(token, base_url, transport=transport)
    except MemberCapabilityError as exc:
        result.app_token_error = str(exc)
        return
    except (httpx.HTTPError, ValueError) as exc:
        result.app_token_error = _unreachable_error(exc)
        return
    result.app_token_ok = True


def _prefix_error(token: str, prefix: str) -> str:
    """Catch an empty field and the common bot/app token mix-up up front."""
    if not token:
        return ERROR_MISSING
    if not token.startswith(prefix):
        return ERROR_WRONG_TOKEN_TYPE
    return ""


def _unreachable_error(exc: Exception) -> str:
    # The token itself may appear in an httpx error message, so only the
    # exception type is reported back to the GUI.
    logger.debug("Slack token verification could not reach Slack: %s", type(exc))
    return ERROR_UNREACHABLE
