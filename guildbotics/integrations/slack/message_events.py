from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from guildbotics.integrations.chat_workflow_status import (
    normalize_workflow_status_metadata,
)
from guildbotics.runtime.chat_service import ChatEvent

#: A user mention as Slack writes it into message text: ``<@U123>``, or
#: ``<@U123|name>``.
MENTION_PATTERN = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND_DIGITS = 6

_CONVERSATIONAL_MESSAGE_SUBTYPES = frozenset(
    {
        "",
        "bot_message",
        "file_share",
        "me_message",
        "thread_broadcast",
    }
)


def ts_time(ts: str) -> datetime:
    """When a message of timestamp ``ts`` occurred: Slack names a message by
    the seconds and microseconds since the epoch it occurred at."""
    seconds, _, fraction = ts.partition(".")
    return _EPOCH + timedelta(
        seconds=int(seconds),
        microseconds=int(
            fraction[:_MICROSECOND_DIGITS].ljust(_MICROSECOND_DIGITS, "0")
        ),
    )


def time_ts(moment: datetime) -> str:
    """``moment`` as the timestamp Slack bounds a history read by."""
    elapsed = moment - _EPOCH
    return f"{elapsed // timedelta(seconds=1)}.{elapsed.microseconds:06d}"


def mentioned_user_ids(text: str) -> list[str]:
    return list(dict.fromkeys(MENTION_PATTERN.findall(text or "")))


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
    try:
        occurred_at = ts_time(message_ts)
    except ValueError:
        return None
    if not is_conversational_message(raw):
        return None
    thread_ts = str(raw.get("thread_ts", "") or "") or message_ts
    text = str(raw.get("text", "") or "")
    return ChatEvent(
        event_id=f"{channel_id}:{message_ts}",
        channel_id=channel_id,
        message_id=message_ts,
        thread_id=thread_ts,
        occurred_at=occurred_at,
        author_id=str(raw.get("user", "") or "") or None,
        text=text,
        mentions=mentioned_user_ids(text),
        is_bot_message=is_bot_message(raw),
        is_thread_reply=thread_ts != message_ts,
        metadata=normalize_workflow_status_metadata(raw.get("metadata")),
    )
