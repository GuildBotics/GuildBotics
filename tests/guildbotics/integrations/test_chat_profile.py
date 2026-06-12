from __future__ import annotations

from guildbotics.integrations.chat_profile import (
    get_chat_profile,
    get_chat_scheduled_posts,
    get_chat_slack_base_url,
    get_chat_subscriptions,
)


class _Person:
    def __init__(self, profile=None, message_channels=None):
        self.profile = profile
        self.message_channels = message_channels if message_channels is not None else []


def test_get_chat_profile_returns_empty_for_non_dict():
    assert get_chat_profile(_Person(profile=None)) == {}
    assert get_chat_profile(_Person(profile="x")) == {}
    assert get_chat_profile(object()) == {}


def test_get_chat_profile_and_collections_normalize():
    person = _Person(
        profile={
            "chat": {
                "subscriptions": [{"channel_id": "C1"}, "bad"],
                "scheduled_posts": [{"name": "a"}, 1],
                "slack_base_url": " https://slack.local/api ",
            }
        }
    )

    assert get_chat_profile(person) == person.profile["chat"]
    assert get_chat_subscriptions(person) == [{"channel_id": "C1"}]
    assert get_chat_scheduled_posts(person) == [{"name": "a"}]
    assert get_chat_slack_base_url(person) == "https://slack.local/api"


def test_get_chat_subscriptions_prefers_message_channels():
    person = _Person(
        profile={"chat": {"subscriptions": [{"channel_id": "OLD"}]}},
        message_channels=[
            {
                "service": "slack",
                "name": "dev-chat",
                "chat": {
                    "enabled": True,
                    "channel_id": "C1",
                    "event_source": "socket_mode",
                },
            },
            {"service": "slack", "name": "ignored-no-chat"},
        ],
    )
    subs = get_chat_subscriptions(person)
    assert subs == [
        {
            "service": "slack",
            "channel_id": "C1",
            "channel_name": "dev-chat",
            "enabled": True,
            "event_source": "socket_mode",
        }
    ]


def test_get_chat_subscriptions_defaults_to_polling_without_timeout():
    person = _Person(
        message_channels=[
            {"service": "slack", "name": "dev-chat", "chat": {"enabled": True}}
        ]
    )
    assert get_chat_subscriptions(person) == [
        {
            "service": "slack",
            "channel_id": "",
            "channel_name": "dev-chat",
            "enabled": True,
            "event_source": "socket_mode",
        }
    ]
