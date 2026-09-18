"""Login comparisons must survive the REST / GraphQL naming of GitHub Apps."""

from __future__ import annotations

import pytest

from guildbotics.entities.message import Message
from guildbotics.entities.team import Person
from guildbotics.integrations.github.github_utils import (
    get_author_type,
    get_person_name,
    normalize_login,
)


def _app_member() -> Person:
    return Person(
        person_id="aiko",
        name="Aiko",
        account_info={"github_username": "aiko-guildbotics-com[bot]"},
    )


@pytest.mark.parametrize(
    ("login", "expected"),
    [
        ("aiko-guildbotics-com[bot]", "aiko-guildbotics-com"),
        ("aiko-guildbotics-com", "aiko-guildbotics-com"),
        ("Aiko-GuildBotics-Com[BOT]", "aiko-guildbotics-com"),
        ("ototadana-bot", "ototadana-bot"),
    ],
)
def test_normalize_login_drops_the_app_suffix_and_case(login, expected):
    assert normalize_login(login) == expected


@pytest.mark.parametrize(
    "login",
    ["aiko-guildbotics-com[bot]", "aiko-guildbotics-com", "Aiko-GuildBotics-Com"],
)
def test_app_member_is_the_assistant_under_both_api_spellings(login):
    """REST says ``<app>[bot]``, GraphQL ``Actor.login`` says ``<app>``."""
    assert get_author_type(_app_member(), login) == Message.ASSISTANT
    assert get_person_name([_app_member()], login) == "Aiko"


def test_other_logins_stay_users():
    assert get_author_type(_app_member(), "ototadana-bot") == Message.USER
    assert get_person_name([_app_member()], "ototadana-bot") == ""
