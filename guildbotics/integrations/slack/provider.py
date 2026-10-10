"""Slack as a provider: the chat, with its Socket Mode listener."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from logging import Logger
from typing import Any

from guildbotics.entities.team import Person, Service, Team
from guildbotics.integrations.chat_profile import get_chat_subscriptions
from guildbotics.integrations.provider import (
    Chat,
    Provider,
    ProviderCheck,
    configured_check,
)
from guildbotics.integrations.slack.slack_chat_service import (
    DEFAULT_BASE_URL,
    SlackApiError,
    SlackChatService,
    probe_app_token,
)
from guildbotics.integrations.slack.slack_socket_listener import (
    SlackSocketEventListener,
)
from guildbotics.runtime.chat_service import ChatCredentialsError
from guildbotics.runtime.context import Context

_SECTION = "slack"
_USER_ID = re.compile(r"^[UW][A-Z0-9]{8,}$")


def base_url(team: Team) -> str:
    """The Web API a project's Slack is reached at: ``base_url`` of
    ``project.services.chat_service``."""
    config = team.project.get_service_config(Service.CHAT_SERVICE)
    return str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")


def _chat(logger: Logger, person: Person, team: Team) -> SlackChatService:
    return SlackChatService(
        logger,
        token=person.get_secret("SLACK_BOT_TOKEN")
        if person.has_secret("SLACK_BOT_TOKEN")
        else "",
        token_name=person.to_person_env_key("SLACK_BOT_TOKEN"),
        app_token=_app_token(person),
        base_url=base_url(team),
    )


def _app_token(person: Person) -> str:
    return (
        person.get_secret("SLACK_APP_TOKEN")
        if person.has_secret("SLACK_APP_TOKEN")
        else ""
    )


def _listener_key(person: Person, team: Team) -> str:
    """Members sharing an app-level token share its one Socket Mode
    connection."""
    token = _app_token(person)
    if not token:
        raise ChatCredentialsError(
            f"Slack App Token is required for person '{person.person_id}'. "
            f"Set environment variable '{person.to_person_env_key('SLACK_APP_TOKEN')}'."
        )
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"{digest}@{base_url(team)}"


def _event_listener(
    logger: Logger, team: Team, persons: list[Person], on_activity: Callable[[], None]
) -> SlackSocketEventListener:
    return SlackSocketEventListener(
        logger=logger,
        app_token=_app_token(persons[0]),
        base_url=base_url(team),
        person_ids=[person.person_id for person in persons],
        on_activity=on_activity,
    )


def _verify(person: Person) -> list[ProviderCheck]:
    if person.person_type == "human" or not get_chat_subscriptions(person):
        return []
    return [
        configured_check(
            _SECTION,
            "slack_credential",
            person,
            key,
            person.to_person_env_key(key),
            person.has_secret(key),
        )
        for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
    ]


async def _diagnose(context: Context, members: list[Person]) -> list[ProviderCheck]:
    checks: list[ProviderCheck] = []
    subscribed = False
    for member in members:
        if member.person_type == "human":
            checks.append(_human_user_id(member))
            continue
        subscriptions = get_chat_subscriptions(member)
        if not subscriptions:
            continue
        subscribed = True
        checks.append(await _app_token_check(context, member))
        checks.extend(await _bot_checks(context, member, subscriptions))
    if not subscribed and any(m.person_type != "human" for m in members):
        checks.append(
            ProviderCheck(
                section=_SECTION,
                code="slack_not_configured",
                status="ok",
                message="Slack channels are not configured; Slack diagnostics were skipped.",
            )
        )
    return checks


def _human_user_id(member: Person) -> ProviderCheck:
    user_id = str(member.account_info.get("slack_user_id", "")).strip()
    if not user_id:
        status, message = (
            "warning",
            "Slack User ID is not configured for this human member.",
        )
    elif _USER_ID.fullmatch(user_id):
        status, message = "ok", "Slack User ID is configured."
    else:
        status, message = "error", "Slack User ID format is invalid."
    return ProviderCheck(
        section=_SECTION,
        code="human_slack_user_id",
        status=status,  # type: ignore[arg-type]
        message=message,
        person_id=member.person_id,
        target=user_id,
    )


async def _app_token_check(context: Context, member: Person) -> ProviderCheck:
    """Validate the Socket Mode app-level token, not just its presence.

    A configured-but-invalid app token (truncated, revoked, wrong app) is the
    common cause of a member silently not receiving events: the bot token can
    still pass ``auth.test`` while Socket Mode fails with ``invalid_auth``.
    ``apps.connections.open`` is the call the event listener makes, and writes
    nothing.
    """
    target = member.to_person_env_key("SLACK_APP_TOKEN")
    token = _app_token(member)
    if not token:
        return ProviderCheck(
            section=_SECTION,
            code="slack_app_token",
            status="error",
            message="Slack App token is required for Socket Mode runtime.",
            person_id=member.person_id,
            target=target,
        )
    try:
        await probe_app_token(token, base_url(context.team))
    except Exception as exc:
        return ProviderCheck(
            section=_SECTION,
            code="slack_app_token_invalid",
            status="error",
            message=_safe_error("Slack App token (Socket Mode) check failed", exc),
            person_id=member.person_id,
            target=target,
            context={"error_type": type(exc).__name__},
        )
    return ProviderCheck(
        section=_SECTION,
        code="slack_app_token",
        status="ok",
        message="Slack App token (Socket Mode) is valid.",
        person_id=member.person_id,
    )


async def _bot_checks(
    context: Context, member: Person, subscriptions: list[dict[str, Any]]
) -> list[ProviderCheck]:
    c = context.clone_for(member)
    try:
        chat = c.get_chat_service()
        identity = await chat.get_bot_identity()
        checks = [
            ProviderCheck(
                section=_SECTION,
                code="slack_bot_auth",
                status="ok",
                message="Slack bot authentication succeeded.",
                person_id=member.person_id,
                context={"bot_user_id": identity.user_id},
            )
        ]
        for sub in subscriptions:
            checks.append(
                await _channel_check(
                    c, sub, member.person_id, identity.display_name or identity.user_id
                )
            )
        return checks
    except Exception as exc:
        return [
            ProviderCheck(
                section=_SECTION,
                code="slack_access",
                status="error",
                message=_safe_error("Slack read-only check failed", exc),
                person_id=member.person_id,
                context={"error_type": type(exc).__name__},
            )
        ]
    finally:
        await c.aclose()


async def _channel_check(
    context: Context, subscription: dict[str, Any], person_id: str, bot_name: str
) -> ProviderCheck:
    chat = context.get_chat_service()
    channel_id = str(subscription.get("channel_id", "") or "").strip()
    channel_name = str(subscription.get("channel_name", "") or "").strip()
    target = channel_id or channel_name
    if not channel_id and channel_name:
        channel_id = await chat.resolve_channel_id(channel_name) or ""
    if not channel_id:
        return ProviderCheck(
            section=_SECTION,
            code="slack_channel",
            status="error",
            message="Slack channel could not be resolved.",
            person_id=person_id,
            target=target,
        )
    try:
        await chat.list_channel_events(channel_id, limit=1)
    except SlackApiError as exc:
        if exc.error != "not_in_channel":
            raise
        # A bot that was never invited is the ordinary first-run state, not
        # a credential problem, so it gets its own actionable check instead
        # of the generic "check your tokens" failure.
        return ProviderCheck(
            section=_SECTION,
            code="slack_channel_not_joined",
            status="error",
            message="Slack bot has not joined the channel.",
            person_id=person_id,
            target=target or channel_id,
            # Named so the GUI can spell out the exact /invite to run
            # instead of a generic instruction.
            context={
                "channel_id": channel_id,
                "channel": channel_name or target or channel_id,
                "bot_name": bot_name,
            },
        )
    return ProviderCheck(
        section=_SECTION,
        code="slack_channel_history",
        status="ok",
        message="Slack channel history was fetched.",
        person_id=person_id,
        target=target or channel_id,
    )


def _safe_error(prefix: str, exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    return f"{prefix}: {message}"


SLACK = Provider(
    name="slack",
    secret_keys=frozenset({"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"}),
    account_info_keys=frozenset({"slack_user_id"}),
    chat=Chat(
        service=_chat, event_listener=_event_listener, listener_key=_listener_key
    ),
    credentialed=lambda person: person.has_secret("SLACK_BOT_TOKEN"),
    verify=_verify,
    diagnose=_diagnose,
)
