from __future__ import annotations

from typing import Any

import pytest

from guildbotics.capabilities import member_activity_events
from guildbotics.entities.team import Person

PR_URL = "https://github.com/owner/repo/pull/544"
TICKET_URL = "local://tickets/acme/team/demo-tickets/T-7"


def _target(kind: str, url: str, label: str, title: str = "") -> dict[str, Any]:
    return {
        "provider": "local",
        "kind": kind,
        "url": url,
        "label": label,
        "title": title,
    }


@pytest.fixture
def recorded(monkeypatch) -> dict[str, Any]:
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(
        member_activity_events,
        "record_correlated_event",
        lambda **kwargs: recorded.update(kwargs),
    )
    return recorded


@pytest.mark.parametrize("read_only", [True, False])
def test_record_member_work_target_names_the_item_and_marks_reads(
    recorded: dict[str, Any], read_only: bool
) -> None:
    member_activity_events.record_member_work_target(
        Person(person_id="aiko", name="Aiko"),
        _target("pull_request", PR_URL, "#544", "  Show the work target  "),
        read_only=read_only,
    )

    assert recorded["event_type"] == "target.recorded"
    assert recorded["payload"] == {
        "pull_request": {
            "label": "#544",
            "title": "Show the work target",
            "url": PR_URL,
        }
    }
    assert recorded["attributes"] == {
        "target.provider": "local",
        "target.action": "inspected" if read_only else "",
        "target.kind": "pull_request",
        "target.label": "#544",
        "target.url": PR_URL,
        "target.title": "Show the work target",
    }


def test_a_ticket_target_is_recorded_as_an_issue_by_its_label(
    recorded: dict[str, Any],
) -> None:
    member_activity_events.record_member_work_target(
        Person(person_id="aiko", name="Aiko"),
        _target("ticket", TICKET_URL, "T-7", "A ticket"),
        read_only=False,
    )

    assert recorded["payload"] == {
        "ticket": {"label": "T-7", "title": "A ticket", "url": TICKET_URL}
    }
    assert recorded["attributes"]["target.kind"] == "ticket"
    assert recorded["attributes"]["target.label"] == "T-7"


def test_record_member_pr_create_event_records_the_created_target(
    recorded: dict[str, Any],
) -> None:
    member_activity_events.record_member_pr_create_event(
        Person(person_id="aiko", name="Aiko"),
        {
            "created": True,
            "target": _target(
                "pull_request", PR_URL, "#403", "  Fix trace ticket chip  "
            ),
        },
    )

    assert recorded["event_type"] == "pull_request.item"
    assert recorded["payload"] == {
        "action": "opened",
        "pull_request": {
            "label": "#403",
            "title": "Fix trace ticket chip",
            "url": PR_URL,
            "merged": False,
        },
    }
    assert recorded["attributes"]["target.label"] == "#403"


def test_record_member_pr_create_event_ignores_a_reused_pull_request(
    recorded: dict[str, Any],
) -> None:
    member_activity_events.record_member_pr_create_event(
        Person(person_id="aiko", name="Aiko"),
        {"created": False, "target": _target("pull_request", PR_URL, "#403")},
    )

    assert recorded == {}


def test_record_member_issue_create_event_builds_github_domain_payload(
    recorded: dict[str, Any],
) -> None:
    member_activity_events.record_member_issue_create_event(
        Person(person_id="aiko", name="Aiko"),
        {"target": _target("ticket", TICKET_URL, "T-7", "  Follow-up  ")},
    )

    assert recorded == {
        "event_type": "ticket.item",
        "payload": {
            "action": "opened",
            "ticket": {"label": "T-7", "title": "Follow-up", "url": TICKET_URL},
        },
        "attributes": {
            "target.provider": "local",
            "target.action": "opened",
            "target.kind": "ticket",
            "target.label": "T-7",
            "target.url": TICKET_URL,
            "target.title": "Follow-up",
        },
        "default_source": "local",
        "person_id": "aiko",
    }


def test_record_member_issue_close_event_reports_the_close_as_activity(
    recorded: dict[str, Any],
) -> None:
    member_activity_events.record_member_issue_close_event(
        Person(person_id="aiko", name="Aiko"),
        {
            "state": "closed",
            "state_changed": True,
            "target": _target("ticket", TICKET_URL, "T-7", "  Capability gap  "),
        },
    )

    assert recorded["event_type"] == "ticket.item"
    assert recorded["payload"] == {
        "action": "closed",
        "ticket": {"label": "T-7", "title": "Capability gap", "url": TICKET_URL},
    }
    assert recorded["attributes"]["target.action"] == "closed"
    assert recorded["attributes"]["target.label"] == "T-7"
    assert recorded["person_id"] == "aiko"


@pytest.mark.parametrize(
    ("state", "state_changed"),
    [("open", True), ("closed", False)],
    ids=["kept-open", "already-closed"],
)
def test_record_member_issue_close_event_ignores_updates_that_close_nothing(
    recorded: dict[str, Any], state: str, state_changed: bool
) -> None:
    member_activity_events.record_member_issue_close_event(
        Person(person_id="aiko", name="Aiko"),
        {
            "state": state,
            "state_changed": state_changed,
            "target": _target("ticket", TICKET_URL, "T-7"),
        },
    )

    assert recorded == {}


def test_record_member_issue_comment_event_keeps_comment_diagnostics_url(
    recorded: dict[str, Any],
) -> None:
    ticket_url = "https://github.com/owner/repo/issues/42"
    member_activity_events.record_member_issue_comment_event(
        Person(person_id="aiko", name="Aiko"),
        {
            "id": "issuecomment-123",
            "url": f"{ticket_url}#issuecomment-123",
            "target": _target("ticket", ticket_url, "#42"),
        },
    )

    assert recorded["event_type"] == "ticket.comment"
    assert recorded["payload"] == {
        "action": "commented",
        "ticket": {"label": "#42", "title": "", "url": ticket_url},
        "comment": {
            "id": "issuecomment-123",
            "url": f"{ticket_url}#issuecomment-123",
        },
    }
    assert recorded["attributes"] == {
        "target.provider": "local",
        "target.action": "commented",
        "target.kind": "ticket",
        "target.label": "#42",
        "target.url": ticket_url,
        "target.title": "",
    }
    assert recorded["person_id"] == "aiko"
