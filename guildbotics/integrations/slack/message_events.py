from __future__ import annotations

from typing import Any

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
