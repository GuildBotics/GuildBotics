"""Slack's descriptor: how members share a connection, where Slack is, and
what a member's configuration must hold."""

from __future__ import annotations

import hashlib
import logging

import pytest

from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.slack.provider import SLACK, base_url
from guildbotics.runtime.chat_service import ChatCredentialsError


def _team(*members: Person, **chat: str) -> Team:
    return Team(
        project=Project(services={"chat_service": {"name": "slack", **chat}}),
        members=list(members),
    )


def _member(person_id: str, *, subscribed: bool = True, **fields) -> Person:
    channels = [{"name": "dev-chat", "chat": {"enabled": True}}] if subscribed else []
    return Person(
        person_id=person_id, name=person_id, message_channels=channels, **fields
    )


def test_members_sharing_an_app_token_share_a_connection(monkeypatch):
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "xapp-shared")
    monkeypatch.setenv("BOB_SLACK_APP_TOKEN", "xapp-shared")
    monkeypatch.setenv("CAROL_SLACK_APP_TOKEN", "xapp-other")
    alice, bob, carol = _member("alice"), _member("bob"), _member("carol")
    team = _team(alice, bob, carol)
    key = SLACK.chat.listener_key  # type: ignore[union-attr]

    assert key(alice, team) == key(bob, team) != key(carol, team)
    assert key(alice, team).startswith(hashlib.sha256(b"xapp-shared").hexdigest())
    assert "xapp-shared" not in key(alice, team)


def test_a_connection_is_per_slack_too(monkeypatch):
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "xapp-shared")
    alice = _member("alice")
    key = SLACK.chat.listener_key  # type: ignore[union-attr]

    assert key(alice, _team(alice)) != key(
        alice, _team(alice, base_url="https://proxy.example.test/slack/")
    )


def test_a_member_without_an_app_token_cannot_connect(monkeypatch):
    monkeypatch.delenv("ALICE_SLACK_APP_TOKEN", raising=False)
    alice = _member("alice")

    with pytest.raises(ChatCredentialsError, match="ALICE_SLACK_APP_TOKEN"):
        SLACK.chat.listener_key(alice, _team(alice))  # type: ignore[union-attr]


def test_slack_is_where_the_projects_chat_service_says():
    assert base_url(_team()) == "https://slack.com/api"
    assert (
        base_url(_team(base_url="https://proxy.example.test/slack/"))
        == "https://proxy.example.test/slack"
    )


def test_a_listener_connects_with_the_first_members_app_token(monkeypatch):
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "xapp-alice")
    alice, bob = _member("alice"), _member("bob")

    listener = SLACK.chat.event_listener(  # type: ignore[union-attr]
        logging.getLogger(__name__),
        _team(alice, bob, base_url="https://proxy.example.test/slack"),
        [alice, bob],
        lambda: None,
    )

    assert listener._app_token == "xapp-alice"
    assert listener._base_url == "https://proxy.example.test/slack"
    assert listener._person_ids == ["alice", "bob"]


def test_a_subscribed_agent_needs_both_tokens(monkeypatch):
    monkeypatch.setenv("ALICE_SLACK_BOT_TOKEN", "xoxb-alice")
    monkeypatch.delenv("ALICE_SLACK_APP_TOKEN", raising=False)

    checks = SLACK.verify(_member("alice"))

    assert [(c.target, c.status) for c in checks] == [
        ("ALICE_SLACK_BOT_TOKEN", "ok"),
        ("ALICE_SLACK_APP_TOKEN", "error"),
    ]
    assert SLACK.verify(_member("bob", subscribed=False)) == []
    assert SLACK.verify(_member("carol", person_type="human")) == []
