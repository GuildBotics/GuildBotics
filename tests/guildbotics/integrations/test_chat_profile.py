from __future__ import annotations

import pytest

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
            "notify_work_stops": False,
            "startup_backfill_minutes": 60,
            "backfill_interval_seconds": 300,
        }
    ]


def test_get_chat_subscriptions_takes_the_channel_name_from_the_channel():
    person = _Person(message_channels=[{"name": "dev-chat", "chat": {"enabled": True}}])
    assert get_chat_subscriptions(person) == [
        {
            "channel_id": "",
            "channel_name": "dev-chat",
            "participation": "strict",
            "notify_work_stops": False,
        }
    ]


@pytest.mark.parametrize(
    "stored, participation",
    [(None, "strict"), ("  ", "strict"), ("unknown", "strict"), (" Social", "social")],
)
def test_a_channel_participates_strictly_unless_it_names_another_policy(
    stored, participation
):
    person = _Person(
        message_channels=[{"name": "dev-chat", "chat": {"participation": stored}}]
    )
    assert get_chat_subscriptions(person)[0]["participation"] == participation


def test_a_channel_whose_chat_is_disabled_is_not_watched():
    person = _Person(
        message_channels=[
            {"name": "quiet", "chat": {"enabled": False, "channel_id": "C9"}},
            {"name": "dev-chat", "chat": {"channel_id": "C1"}},
        ]
    )
    assert [sub["channel_id"] for sub in get_chat_subscriptions(person)] == ["C1"]


@pytest.mark.parametrize("setting", [None, False, True, "false", "true", 1])
def test_work_stop_notifications_require_an_explicit_true(setting):
    person = _Person(message_channels=[{"chat": {"notify_work_stops": setting}}])
    assert get_chat_subscriptions(person)[0]["notify_work_stops"] is (setting is True)
