from __future__ import annotations

from typing import Any


def get_chat_profile(person: Any) -> dict[str, Any]:
    """Return the normalized `person.profile.chat` mapping."""
    profile = getattr(person, "profile", {}) or {}
    if not isinstance(profile, dict):
        return {}
    chat = profile.get("chat", {}) or {}
    return chat if isinstance(chat, dict) else {}


def get_chat_subscriptions(person: Any) -> list[dict[str, Any]]:
    return _subscriptions_from_message_channels(person)


def get_chat_scheduled_posts(person: Any) -> list[dict[str, Any]]:
    chat = get_chat_profile(person)
    posts = chat.get("scheduled_posts", [])
    if not isinstance(posts, list):
        return []
    return [item for item in posts if isinstance(item, dict)]


def get_chat_slack_base_url(person: Any) -> str | None:
    chat = get_chat_profile(person)
    raw = chat.get("slack_base_url", "")
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _subscriptions_from_message_channels(person: Any) -> list[dict[str, Any]]:
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
    service = str(_get(ch, "service", "") or "").strip().lower()
    if not service:
        return None

    chat_cfg = _as_dict(_get(ch, "chat", {}))
    if not chat_cfg:
        channel_info = _as_dict(_get(ch, "channel_info", {}))
        chat_cfg = _as_dict(channel_info.get("chat", {}))
    if not chat_cfg:
        return None

    enabled = bool(chat_cfg.get("enabled", True))
    channel_name = str(chat_cfg.get("channel_name", "") or "").strip()
    if not channel_name:
        channel_name = str(_get(ch, "name", "") or "").strip()
    channel_id = str(chat_cfg.get("channel_id", "") or "").strip()
    if not channel_id:
        channel_info = _as_dict(_get(ch, "channel_info", {}))
        channel_id = str(channel_info.get("channel_id", "") or "").strip()

    event_source = str(chat_cfg.get("event_source", "") or "").strip().lower()
    if not event_source:
        event_source = "socket_mode"

    item = {
        "service": service,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "enabled": enabled,
        "event_source": event_source,
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
