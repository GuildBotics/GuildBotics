from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast

from guildbotics.capabilities.chat_updates import ensure_chat_current
from guildbotics.capabilities.task_runs import RunStore, current_run_id
from guildbotics.entities.team import Person
from guildbotics.runtime.chat_service import (
    SEMANTIC_REACTIONS,
    ChatCredentialsError,
    ChatEvent,
    ChatMessageRef,
    ChatPostResult,
    ChatService,
    SemanticReaction,
)
from guildbotics.runtime.integration_factory import MemberCapabilityError
from guildbotics.runtime.member_invocation import current_member_invocation


class MemberChatCapabilityService:
    """External chat write boundary for a configured GuildBotics member."""

    def __init__(
        self, person: Person, chat_service: ChatService, service_name: str
    ) -> None:
        self.person = person
        self.chat_service = chat_service
        self.service_name = service_name

    async def aclose(self) -> None:
        close = getattr(self.chat_service, "aclose", None)
        if callable(close):
            await close()

    async def check_credentials(self) -> dict[str, Any]:
        """Try each credential of the member's chat, naming why one fails
        without its value."""
        checks = await self.chat_service.check_credentials()
        statuses = {check.status for check in checks}
        if "failed" in statuses:
            overall = "failed"
        elif statuses <= {"unconfigured"} and checks:
            overall = "unconfigured"
        else:
            overall = "ok"
        return {
            "service": self.service_name,
            "status": overall,
            "credentials": [
                {
                    "name": check.name,
                    "status": check.status,
                    **({"error": _safe_chat_error(check.error)} if check.error else {}),
                }
                for check in checks
            ],
        }

    async def identity(self) -> dict[str, Any]:
        try:
            identity = await self.chat_service.get_bot_identity()
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        return {
            "service": self.service_name,
            "user_id": identity.user_id,
            "display_name": identity.display_name,
            "person_id": self.person.person_id,
        }

    async def resolve_channel(self, channel_name: str) -> dict[str, Any]:
        """Name the channel ``channel_name`` is, without reading or writing it;
        ``channel_id`` is empty when there is none of that name."""
        try:
            channel_id = await self.chat_service.resolve_channel_id(channel_name)
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        return {
            "service": self.service_name,
            "channel_name": channel_name,
            "channel_id": channel_id or "",
        }

    async def inspect_channel(
        self,
        *,
        channel_id: str | None,
        channel_name: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> dict[str, Any]:
        resolved_channel_id = await self._resolve_channel(channel_id, channel_name)
        try:
            page = await self.chat_service.list_channel_events(
                resolved_channel_id, since=since, until=until, limit=limit
            )
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        return {
            "service": self.service_name,
            "mode": "channel",
            "channel_id": resolved_channel_id,
            "channel_name": channel_name or "",
            "since": since.isoformat() if since else "",
            "until": until.isoformat() if until else "",
            "next_cursor": page.cursor or "",
            "messages": _events_payload(page.events),
        }

    async def inspect_thread(
        self,
        *,
        channel_id: str | None,
        channel_name: str | None,
        thread_id: str | None,
        message_url: str | None = None,
        limit: int,
    ) -> dict[str, Any]:
        ref = await self._reference(channel_id, channel_name, thread_id, message_url)
        try:
            page = await self.chat_service.list_thread_events(
                ref.channel_id, thread_id=ref.thread_id, limit=limit
            )
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        return {
            "service": self.service_name,
            "mode": "thread",
            "channel_id": ref.channel_id,
            "channel_name": channel_name or "",
            "thread_id": ref.thread_id,
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
        ensure_chat_current(self.person.person_id)
        try:
            result = await self.chat_service.post_message(
                resolved_channel_id, rendered_body
            )
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        return self._posted(result, rendered_body)

    async def reply(
        self,
        *,
        channel_id: str | None,
        channel_name: str | None,
        thread_id: str | None,
        message_url: str | None = None,
        body: str,
    ) -> dict[str, Any]:
        ref = await self._reference(channel_id, channel_name, thread_id, message_url)
        rendered_body = self._render_participant_text(body)
        ensure_chat_current(self.person.person_id)
        try:
            result = await self.chat_service.post_message(
                ref.channel_id, rendered_body, thread_id=ref.thread_id
            )
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        return self._posted(result, rendered_body)

    def _posted(self, result: ChatPostResult, text: str) -> dict[str, Any]:
        return {
            "service": self.service_name,
            "channel_id": result.channel_id,
            "message_id": result.message_id,
            "thread_id": result.thread_id,
            "occurred_at": result.occurred_at.isoformat(),
            "text": text,
            "posted": True,
        }

    async def add_reaction(
        self,
        *,
        channel_id: str | None,
        channel_name: str | None,
        message_id: str | None,
        message_url: str | None = None,
        reaction: str,
    ) -> dict[str, Any]:
        if reaction not in SEMANTIC_REACTIONS:
            raise MemberCapabilityError(f"Unsupported chat reaction: {reaction}")
        semantic_reaction = cast(SemanticReaction, reaction)
        ref = await self._reference(channel_id, channel_name, message_id, message_url)
        ensure_chat_current(self.person.person_id)
        try:
            await self.chat_service.add_reaction(
                ref.channel_id, ref.message_id, semantic_reaction
            )
        except Exception as exc:
            raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        payload = {
            "service": self.service_name,
            "channel_id": ref.channel_id,
            "message_id": ref.message_id,
            "reaction": semantic_reaction,
            "reacted": True,
        }
        RunStore().append_evidence(current_run_id(), "chat_reaction", payload)
        return payload

    async def _reference(
        self,
        channel_id: str | None,
        channel_name: str | None,
        message_id: str | None,
        message_url: str | None = None,
    ) -> ChatMessageRef:
        """The message ``message_url`` names, else ``message_id`` (which is
        also its thread's) in the channel given."""
        if message_url:
            try:
                return self.chat_service.parse_message_url(message_url)
            except Exception as exc:
                raise MemberCapabilityError(_safe_chat_error(exc)) from exc
        if not message_id:
            raise MemberCapabilityError("A message id or message URL is required.")
        return ChatMessageRef(
            channel_id=await self._resolve_channel(channel_id, channel_name),
            message_id=message_id,
            thread_id=message_id,
        )

    async def _resolve_channel(
        self, channel_id: str | None, channel_name: str | None
    ) -> str:
        if channel_id:
            return channel_id
        if not channel_name:
            raise MemberCapabilityError(
                "Either channel_id or channel_name is required."
            )
        resolved = (await self.resolve_channel(channel_name))["channel_id"]
        if not resolved:
            raise MemberCapabilityError(f"Chat channel was not found: {channel_name}")
        return resolved

    def _render_participant_text(self, body: str) -> str:
        labels = _load_participant_labels()
        if not labels:
            return body
        return self.chat_service.render_participant_text(body, labels)


def _events_payload(events: list[ChatEvent]) -> list[dict[str, Any]]:
    return [event.payload() for event in sorted(events, key=lambda e: e.position)]


def _load_participant_labels() -> dict[str, str]:
    raw = current_member_invocation().participant_labels.strip()
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


def _safe_chat_error(exc: Exception | str) -> str:
    text = str(exc)
    if isinstance(exc, ChatCredentialsError):
        # Written for the member: it names where a credential goes, no value.
        return text
    upper = text.upper()
    if any(
        marker in upper for marker in ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY")
    ):
        return "Chat credential could not be resolved or used safely."
    return text or "Chat capability command failed."
