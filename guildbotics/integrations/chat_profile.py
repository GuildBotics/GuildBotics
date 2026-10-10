from __future__ import annotations

from typing import Any


def get_chat_subscriptions(person: Any) -> list[dict[str, Any]]:
    """The channels ``person`` watches: those of its ``message_channels``
    whose chat is enabled."""
    channels = getattr(person, "message_channels", []) or []
    if not isinstance(channels, list):
        return []

    out: list[dict[str, Any]] = []
    for ch in channels:
        item = _channel_to_subscription(ch)
        if item is not None:
            out.append(item)
    return out


def _channel_to_subscription(ch: Any) -> dict[str, Any] | None:
    chat_cfg = _as_dict(_get(ch, "chat", {}))
    if not chat_cfg:
        channel_info = _as_dict(_get(ch, "channel_info", {}))
        chat_cfg = _as_dict(channel_info.get("chat", {}))
    if not chat_cfg or not bool(chat_cfg.get("enabled", True)):
        return None

    channel_name = str(chat_cfg.get("channel_name", "") or "").strip()
    if not channel_name:
        channel_name = str(_get(ch, "name", "") or "").strip()
    channel_id = str(chat_cfg.get("channel_id", "") or "").strip()
    if not channel_id:
        channel_info = _as_dict(_get(ch, "channel_info", {}))
        channel_id = str(channel_info.get("channel_id", "") or "").strip()

    item = {
        "channel_id": channel_id,
        "channel_name": channel_name,
    }
    for key in (
        "participation",
        "startup_backfill_minutes",
        "backfill_interval_seconds",
        "backfill_overlap_seconds",
        "backfill_limit",
    ):
        if key in chat_cfg:
            item[key] = chat_cfg[key]
    return item


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
