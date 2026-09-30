"""Login comparisons must survive the REST / GraphQL naming of GitHub Apps."""

from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from guildbotics.entities.message import Message
from guildbotics.entities.team import Person
from guildbotics.integrations.github.github_utils import (
    create_github_app_jwt,
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


def test_github_app_jwt_preserves_rs256_signature_and_claims(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    now = 1_800_000_000
    monkeypatch.setattr(
        "guildbotics.integrations.github.github_utils.time.time", lambda: now
    )

    token = create_github_app_jwt("12345", pem)

    claims = jwt.decode(
        token,
        key.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    assert claims == {"iss": "12345", "iat": now - 60, "exp": now + 540}
