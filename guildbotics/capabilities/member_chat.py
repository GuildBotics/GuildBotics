from __future__ import annotations

import json
import os
from logging import Logger
from typing import Any, cast

import httpx

from guildbotics.capabilities.member_github import MemberCapabilityError
from guildbotics.entities.team import Person, Team
from guildbotics.integrations.chat_profile import get_chat_slack_base_url
from guildbotics.integrations.chat_service import (
    SEMANTIC_REACTIONS,
    ChatEvent,
    ChatService,
    SemanticReaction,
)

SLACK_BOT_TOKEN_KEY = "SLACK_BOT_TOKEN"
SLACK_APP_TOKEN_KEY = "SLACK_APP_TOKEN"
CHAT_PARTICIPANT_LABELS_ENV = "GUILDBOTICS_CHAT_PARTICIPANT_LABELS"


class MemberChatCapabilityService:
    """External chat write boundary for a configured GuildBotics member."""

    def __init__(
        self,
        person: Person,
        team: Team,
        logger: Logger,
        chat_service: ChatService | None,
        *,
        service_name: str = "slack",
    ) -> None:
        self.person = person
        self.team = team
        self.logger = logger
        # May be None for credential-only use when the member has an app token
        # but no bot token (the chat service factory requires a bot token). Write
        # operations resolve it through ``_chat`` and fail closed if absent.
        self.chat_service = chat_service
        self.service_name = service_name

    def _chat(self) -> ChatService:
        if self.chat_service is None:
            raise MemberCapabilityError("Slack bot token is not configured.")
        return self.chat_service

    async def aclose(self) -> None:
        close = getattr(self.chat_service, "aclose", None)
        if callable(close):
            await close()

    async def check_credentials(self) -> dict[str, Any]:
        """Validate both the bot token and the Socket Mode app-level token.

        The bot token (used for Web API writes) is probed with ``auth.test`` via
        ``get_bot_identity``. The app-level token (used only for Socket Mode event
        reception) cannot be validated with ``auth.test``; it is probed with
        ``apps.connections.open``, the same call the event listener makes. Failures
        surface the Slack error code (e.g. ``invalid_auth``) without leaking values.
        """
        bot_status, bot_error = await self._check_bot_token()
        app_status, app_error = await self._check_app_token()
        statuses = (bot_status, app_status)
        if "failed" in statuses:
            overall = "failed"
        elif all(status == "unconfigured" for status in statuses):
            overall = "unconfigured"
        else:
            overall = "ok"
        result: dict[str, Any] = {
            "service": self.service_name,
            "status": overall,
            "bot_token": bot_status,
            "app_token": app_status,
        }
        if bot_error:
            result["bot_token_error"] = bot_error
        if app_error:
            result["app_token_error"] = app_error
        return result

    async def _check_bot_token(self) -> tuple[str, str]:
        if not self.person.has_secret(SLACK_BOT_TOKEN_KEY) or self.chat_service is None:
            return "unconfigured", ""
        try:
            await self.chat_service.get_bot_identity()
        except Exception as exc:
            return "failed", _safe_chat_error(exc)
        return "ok", ""

    async def _check_app_token(self) -> tuple[str, str]:
        if not self.person.has_secret(SLACK_APP_TOKEN_KEY):
            return "unconfigured", ""
        try:
            await probe_slack_app_token(
                self.person.get_secret(SLACK_APP_TOKEN_KEY),
                get_chat_slack_base_url(self.person),
            )
        except Exception as exc:
            return "failed", _safe_chat_error(exc)
        return "ok", ""

    async def identity(self) -> dict[str, Any]:
        try:
            identity = await self._chat().get_bot_identity()
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        return {
            "service": self.service_name,
            "user_id": identity.user_id,
            "display_name": identity.display_name,
            "person_id": self.person.person_id,
        }

    async def inspect_channel(
        self,
        *,
        channel_id: str | None,
        channel_name: str | None,
        oldest_ts: str | None,
        latest_ts: str | None,
        limit: int,
    ) -> dict[str, Any]:
        resolved_channel_id = await self._resolve_channel(channel_id, channel_name)
        try:
            page = await self._chat().list_channel_events(
                resolved_channel_id,
                oldest_ts=oldest_ts,
                latest_ts=latest_ts,
                limit=limit,
            )
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        return {
            "service": self.service_name,
            "mode": "channel",
            "channel_id": resolved_channel_id,
            "channel_name": channel_name or "",
            "oldest_ts": oldest_ts or "",
            "latest_ts": latest_ts or "",
            "next_cursor": page.cursor or "",
            "messages": _events_payload(page.events),
        }

    async def inspect_thread(
        self,
        *,
        channel_id: str | None,
        channel_name: str | None,
        thread_ts: str,
        limit: int,
    ) -> dict[str, Any]:
        resolved_channel_id = await self._resolve_channel(channel_id, channel_name)
        try:
            page = await self._chat().list_thread_events(
                resolved_channel_id,
                thread_ts=thread_ts,
                limit=limit,
            )
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        return {
            "service": self.service_name,
            "mode": "thread",
            "channel_id": resolved_channel_id,
            "channel_name": channel_name or "",
            "thread_ts": thread_ts,
            "next_cursor": page.cursor or "",
            "messages": _events_payload(page.events),
        }

    async def post(
        self,
        *,
        channel_id: str | None,
        channel_name: str | None,
        body: str,
    ) -> dict[str, Any]:
        resolved_channel_id = await self._resolve_channel(channel_id, channel_name)
        rendered_body = self._render_participant_text(body)
        try:
            result = await self._chat().post_message(resolved_channel_id, rendered_body)
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        return {
            "service": self.service_name,
            "channel_id": result.channel_id,
            "message_ts": result.message_ts,
            "thread_ts": result.thread_ts,
            "text": rendered_body,
            "posted": True,
        }

    async def reply(
        self,
        *,
        channel_id: str | None,
        channel_name: str | None,
        thread_ts: str,
        body: str,
    ) -> dict[str, Any]:
        resolved_channel_id = await self._resolve_channel(channel_id, channel_name)
        rendered_body = self._render_participant_text(body)
        try:
            result = await self._chat().post_message(
                resolved_channel_id, rendered_body, thread_ts=thread_ts
            )
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        return {
            "service": self.service_name,
            "channel_id": result.channel_id,
            "message_ts": result.message_ts,
            "thread_ts": result.thread_ts,
            "text": rendered_body,
            "posted": True,
        }

    async def add_reaction(
        self,
        *,
        channel_id: str | None,
        channel_name: str | None,
        message_ts: str,
        reaction: str,
    ) -> dict[str, Any]:
        if reaction not in SEMANTIC_REACTIONS:
            raise MemberCapabilityError(f"Unsupported chat reaction: {reaction}")
        semantic_reaction = cast(SemanticReaction, reaction)
        resolved_channel_id = await self._resolve_channel(channel_id, channel_name)
        try:
            await self._chat().add_reaction(
                resolved_channel_id, message_ts, semantic_reaction
            )
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        return {
            "service": self.service_name,
            "channel_id": resolved_channel_id,
            "message_ts": message_ts,
            "reaction": semantic_reaction,
            "reacted": True,
        }

    async def _resolve_channel(
        self, channel_id: str | None, channel_name: str | None
    ) -> str:
        if channel_id:
            return channel_id
        if not channel_name:
            raise MemberCapabilityError(
                "Either channel_id or channel_name is required."
            )
        try:
            resolved = await self._chat().resolve_channel_id(channel_name)
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        if not resolved:
            raise MemberCapabilityError(f"Chat channel was not found: {channel_name}")
        return resolved

    def _render_participant_text(self, body: str) -> str:
        labels = _load_participant_labels()
        if not labels:
            return body
        render = getattr(self._chat(), "render_participant_text", None)
        if not callable(render):
            return body
        return str(render(body, labels))


def _events_payload(events: list[ChatEvent]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": event.event_id,
            "channel_id": event.channel_id,
            "message_ts": event.message_ts,
            "thread_ts": event.thread_ts,
            "author_id": event.author_id or "",
            "text": event.text,
            "mentions": event.mentions,
            "is_bot_message": event.is_bot_message,
            "is_thread_reply": event.is_thread_reply,
        }
        for event in sorted(events, key=lambda item: item.message_ts)
    ]


def _load_participant_labels() -> dict[str, str]:
    raw = os.getenv(CHAT_PARTICIPANT_LABELS_ENV, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(user_id): str(label)
        for user_id, label in value.items()
        if str(user_id) and str(label)
    }


async def probe_slack_app_token(app_token: str, base_url: str | None) -> None:
    """Validate a Slack app-level token via ``apps.connections.open``.

    Raises ``MemberCapabilityError`` carrying only the Slack error code so callers
    can surface it safely (the code, e.g. ``invalid_auth``, is not a secret).
    """
    url = (base_url or "https://slack.com/api").rstrip("/") + "/apps.connections.open"
    async with httpx.AsyncClient(
        timeout=10.0, headers={"Authorization": f"Bearer {app_token}"}
    ) as client:
        response = await client.post(url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or not payload.get("ok", False):
        error = (
            payload.get("error", "unknown_error")
            if isinstance(payload, dict)
            else "invalid_json"
        )
        raise MemberCapabilityError(str(error))


def _safe_chat_error(exc: Exception) -> str:
    text = str(exc)
    upper = text.upper()
    if any(
        marker in upper for marker in ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY")
    ):
        return "Chat credential could not be resolved or used safely."
    return text or "Chat capability command failed."
