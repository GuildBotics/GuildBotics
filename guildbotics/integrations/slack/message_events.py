from __future__ import annotations

import re
from typing import Any

from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.chat_workflow_status import (
    normalize_workflow_status_metadata,
)

#: A user mention as Slack writes it into message text: ``<@U123>``.
MENTION_PATTERN = re.compile(r"<@([A-Z0-9]+)>")

_CONVERSATIONAL_MESSAGE_SUBTYPES = frozenset(
    {
        "",
        "bot_message",
        "file_share",
        "me_message",
        "thread_broadcast",
    }
)


def get_message_subtype(raw: dict[str, Any]) -> str:
    return str(raw.get("subtype", "") or "")


def is_conversational_message(raw: dict[str, Any]) -> bool:
    return get_message_subtype(raw) in _CONVERSATIONAL_MESSAGE_SUBTYPES


def is_bot_message(raw: dict[str, Any]) -> bool:
    return bool(raw.get("bot_id")) or get_message_subtype(raw) == "bot_message"


def chat_event(channel_id: str, raw: dict[str, Any]) -> ChatEvent | None:
    """A Slack message in ``channel_id`` as a chat event.

    The Web API's history and Socket Mode's events carry messages in the same
    shape. A message that is not conversational, or has no timestamp to name
    it by, is no event.
    """
    message_ts = str(raw.get("ts", "") or "")
    if not message_ts or not is_conversational_message(raw):
        return None
    thread_ts = str(raw.get("thread_ts", "") or "") or message_ts
    text = str(raw.get("text", "") or "")
    return ChatEvent(
        event_id=f"{channel_id}:{message_ts}",
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=thread_ts,
        author_id=str(raw.get("user", "") or "") or None,
        text=text,
        mentions=MENTION_PATTERN.findall(text),
        is_bot_message=is_bot_message(raw),
        is_thread_reply=thread_ts != message_ts,
        metadata=normalize_workflow_status_metadata(raw.get("metadata")),
    )
