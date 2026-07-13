from __future__ import annotations

from typing import Any

from guildbotics.capabilities import member_activity_events
from guildbotics.entities.team import Person


def test_record_member_issue_create_event_builds_github_domain_payload(
    monkeypatch,
) -> None:
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(
        member_activity_events,
        "record_correlated_event",
        lambda **kwargs: recorded.update(kwargs),
    )

    member_activity_events.record_member_issue_create_event(
        Person(person_id="aiko", name="Aiko"),
        {
            "issue_number": 290,
            "issue_title": "Follow-up",
            "repo": "owner/repo",
            "issue_url": "https://github.com/owner/repo/issues/290",
        },
    )

    assert recorded == {
        "event_type": "github.issue",
        "payload": {
            "action": "opened",
            "issue": {
                "number": 290,
                "title": "Follow-up",
                "html_url": "https://github.com/owner/repo/issues/290",
            },
        },
        "attributes": {
            "github.action": "opened",
            "github.kind": "issue",
            "github.number": 290,
            "github.repo": "owner/repo",
            "github.url": "https://github.com/owner/repo/issues/290",
        },
        "default_source": "github",
        "person_id": "aiko",
    }


def test_record_member_issue_comment_event_keeps_comment_diagnostics_url(
    monkeypatch,
) -> None:
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(
        member_activity_events,
        "record_correlated_event",
        lambda **kwargs: recorded.update(kwargs),
    )

    member_activity_events.record_member_issue_comment_event(
        Person(person_id="aiko", name="Aiko"),
        {
            "comment_id": 123,
            "comment_url": "https://github.com/owner/repo/issues/42#issuecomment-123",
            "issue_number": 42,
            "repo": "owner/repo",
            "issue_url": "https://github.com/owner/repo/issues/42",
        },
    )

    assert recorded["event_type"] == "github.issue_comment"
    assert recorded["payload"] == {
        "action": "commented",
        "issue": {
            "number": 42,
            "html_url": "https://github.com/owner/repo/issues/42",
        },
        "comment": {
            "id": 123,
            "html_url": "https://github.com/owner/repo/issues/42#issuecomment-123",
        },
    }
    assert recorded["attributes"] == {
        "github.action": "commented",
        "github.kind": "issue",
        "github.number": 42,
        "github.repo": "owner/repo",
        "github.url": "https://github.com/owner/repo/issues/42",
    }
    assert recorded["person_id"] == "aiko"
