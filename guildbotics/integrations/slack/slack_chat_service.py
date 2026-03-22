from __future__ import annotations

import re
from logging import Logger
from typing import Any

import httpx

from guildbotics.integrations.chat_service import (
    ChatEvent,
    ChatEventPage,
    ChatIdentity,
    ChatPostResult,
    ChatService,
    SemanticReaction,
)

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
_EPHEMERAL_PARTICIPANT_LABEL_RE = re.compile(r"^(?:user|agent)_\d+$", re.IGNORECASE)
_SLACK_REACTION_MAP: dict[SemanticReaction, str] = {
    "ack": "white_check_mark",
    "agree": "thumbsup",
    "celebrate": "tada",
    "support": "heart",
}


class SlackChatService(ChatService):
    """Slack Web API-backed chat service (MVP subset)."""

    def __init__(
        self,
        logger: Logger,
        *,
        token: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._logger = logger
        self._base_url = (base_url or "https://slack.com/api").rstrip("/")
        self._token = token or ""
        self._client = client
        self._owns_client = client is None
        self._channel_name_cache: dict[str, str] = {}

    async def get_bot_identity(self) -> ChatIdentity:
        payload = await self._post_form("auth.test", {})
        return ChatIdentity(
            user_id=str(payload.get("user_id", "")),
            display_name=str(payload.get("user", "")),
        )

    async def list_channel_events(
        self,
        channel_id: str,
        *,
        cursor: str | None = None,
        oldest_ts: str | None = None,
        limit: int = 100,
    ) -> ChatEventPage:
        form: dict[str, str] = {"channel": channel_id, "limit": str(limit)}
        if cursor:
            form["cursor"] = cursor
        if oldest_ts:
            form["oldest"] = oldest_ts
        payload = await self._post_form("conversations.history", form)
        messages = payload.get("messages", [])
        events: list[ChatEvent] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            event = self._to_event(channel_id, item)
            if event is not None:
                events.append(event)
        metadata = payload.get("response_metadata", {})
        next_cursor = None
        if isinstance(metadata, dict):
            next_cursor = _str_or_none(metadata.get("next_cursor"))
        page_oldest = None
        if events:
            page_oldest = max((ev.message_ts for ev in events), default=None)
        return ChatEventPage(events=events, cursor=next_cursor, oldest_ts=page_oldest)

    async def resolve_channel_id(self, channel_name: str) -> str | None:
        name = channel_name.strip().lstrip("#")
        if not name:
            return None
        cached = self._channel_name_cache.get(name)
        if cached:
            return cached

        cursor: str | None = None
        while True:
            form: dict[str, str] = {
                "limit": "1000",
                "exclude_archived": "true",
                "types": "public_channel,private_channel",
            }
            if cursor:
                form["cursor"] = cursor
            payload = await self._post_form("conversations.list", form)
            channels = payload.get("channels", [])
            for item in channels:
                if not isinstance(item, dict):
                    continue
                cid = _str_or_none(item.get("id"))
                if not cid:
                    continue
                raw_name = str(item.get("name", "") or "")
                normalized = str(item.get("name_normalized", "") or "")
                if raw_name == name or normalized == name:
                    self._channel_name_cache[name] = cid
                    return cid
            metadata = payload.get("response_metadata", {})
            next_cursor = None
            if isinstance(metadata, dict):
                next_cursor = _str_or_none(metadata.get("next_cursor"))
            if not next_cursor:
                return None
            cursor = next_cursor

    async def post_message(
        self, channel_id: str, text: str, *, thread_ts: str | None = None
    ) -> ChatPostResult:
        form: dict[str, str] = {"channel": channel_id, "text": text}
        if thread_ts:
            form["thread_ts"] = thread_ts
        payload = await self._post_form("chat.postMessage", form)
        ts = str(payload.get("ts", ""))
        return ChatPostResult(channel_id=channel_id, message_ts=ts, thread_ts=thread_ts or ts)

    async def add_reaction(
        self, channel_id: str, message_ts: str, reaction: str
    ) -> None:
        if reaction not in _SLACK_REACTION_MAP:
            raise RuntimeError(f"Unsupported semantic reaction for Slack: {reaction}")
        await self._post_form(
            "reactions.add",
            {
                "channel": channel_id,
                "timestamp": message_ts,
                "name": _SLACK_REACTION_MAP[reaction],
            },
        )

    def normalize_participant_text(
        self, text: str, participant_labels: dict[str, str]
    ) -> str:
        def repl(match: re.Match[str]) -> str:
            user_id = match.group(1)
            label = participant_labels.get(user_id, "participant")
            return f"@{label}"

        return _MENTION_RE.sub(repl, text or "")

    def render_participant_text(
        self, text: str, participant_labels: dict[str, str]
    ) -> str:
        ephemeral_labels = {
            label.casefold()
            for label in participant_labels.values()
            if label and _EPHEMERAL_PARTICIPANT_LABEL_RE.match(label)
        }
        label_to_user_id = {
            label.casefold(): user_id
            for user_id, label in participant_labels.items()
            if user_id and label and not _EPHEMERAL_PARTICIPANT_LABEL_RE.match(label)
        }

        def repl(match: re.Match[str]) -> str:
            label = match.group(1).strip().casefold()
            if label in ephemeral_labels:
                return match.group(1)
            user_id = label_to_user_id.get(label)
            if not user_id:
                return match.group(0)
            return f"<@{user_id}>"

        return re.sub(r"@([A-Za-z0-9_]+)", repl, text or "")

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _post_form(self, method: str, form: dict[str, str]) -> dict[str, Any]:
        client = self._get_client()
        response = await client.post(f"{self._base_url}/{method}", data=form)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Slack API '{method}' returned non-object JSON.")
        if not payload.get("ok", False):
            error = payload.get("error", "unknown_error")
            raise RuntimeError(f"Slack API '{method}' failed: {error}")
        return payload

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            if not self._token:
                raise RuntimeError("Slack bot token is not configured.")
            headers = {}
            headers["Authorization"] = f"Bearer {self._token}"
            self._client = httpx.AsyncClient(timeout=10.0, headers=headers)
            self._owns_client = True
        return self._client

    def _to_event(self, channel_id: str, raw: dict[str, Any]) -> ChatEvent | None:
        subtype = str(raw.get("subtype", ""))
        if subtype in {"message_changed", "message_deleted"}:
            return None
        author_id = _str_or_none(raw.get("user"))
        text = str(raw.get("text", "") or "")
        thread_ts = _str_or_none(raw.get("thread_ts")) or str(raw.get("ts", "") or "")
        message_ts = str(raw.get("ts", "") or "")
        event_id = f"{channel_id}:{message_ts}"
        is_bot_message = bool(raw.get("bot_id")) or subtype == "bot_message"
        return ChatEvent(
            event_id=event_id,
            channel_id=channel_id,
            message_ts=message_ts,
            thread_ts=thread_ts,
            author_id=author_id,
            text=text,
            mentions=_extract_mentions(text),
            is_bot_message=is_bot_message,
            is_thread_reply=thread_ts != message_ts,
        )

def _extract_mentions(text: str) -> list[str]:
    return _MENTION_RE.findall(text or "")


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s else None
