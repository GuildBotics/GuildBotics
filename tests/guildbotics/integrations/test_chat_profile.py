from __future__ import annotations

from guildbotics.integrations.chat_profile import get_chat_subscriptions


class _Person:
    def __init__(self, message_channels=None):
        self.message_channels = message_channels if message_channels is not None else []


def test_get_chat_subscriptions_reads_message_channels():
    person = _Person(
        message_channels=[
            {
                "name": "dev-chat",
                "chat": {
                    "enabled": True,
                    "channel_id": "C1",
                    "participation": "social",
                    "startup_backfill_minutes": 60,
                    "backfill_interval_seconds": 300,
                },
            },
            {"name": "ignored-no-chat"},
        ],
    )
    subs = get_chat_subscriptions(person)
    assert subs == [
        {
            "channel_id": "C1",
            "channel_name": "dev-chat",
            "participation": "social",
            "startup_backfill_minutes": 60,
            "backfill_interval_seconds": 300,
        }
    ]


def test_get_chat_subscriptions_takes_the_channel_name_from_the_channel():
    person = _Person(message_channels=[{"name": "dev-chat", "chat": {"enabled": True}}])
    assert get_chat_subscriptions(person) == [
        {"channel_id": "", "channel_name": "dev-chat"}
    ]


def test_a_channel_whose_chat_is_disabled_is_not_watched():
    person = _Person(
        message_channels=[
            {"name": "quiet", "chat": {"enabled": False, "channel_id": "C9"}},
            {"name": "dev-chat", "chat": {"channel_id": "C1"}},
        ]
    )
    assert [sub["channel_id"] for sub in get_chat_subscriptions(person)] == ["C1"]
