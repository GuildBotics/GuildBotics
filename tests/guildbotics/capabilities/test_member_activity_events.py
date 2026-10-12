from __future__ import annotations

from typing import Any

import pytest

from guildbotics.capabilities import member_activity_events
from guildbotics.entities.team import Person

PR_URL = "https://github.com/owner/repo/pull/544"
TICKET_URL = "local://tickets/acme/team/demo-tickets/T-7"


def _target(kind: str, url: str, label: str, title: str = "") -> dict[str, Any]:
    return {"kind": kind, "url": url, "label": label, "title": title}


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

    assert recorded["event_type"] == "github.work_target"
    assert recorded["payload"] == {
        "pull_request": {
            "label": "#544",
            "title": "Show the work target",
            "url": PR_URL,
        }
    }
    assert recorded["attributes"] == {
        "github.action": "inspected" if read_only else "",
        "github.kind": "pull_request",
        "github.label": "#544",
        "github.url": PR_URL,
        "github.title": "Show the work target",
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
        "issue": {"label": "T-7", "title": "A ticket", "url": TICKET_URL}
    }
    assert recorded["attributes"]["github.kind"] == "ticket"
    assert recorded["attributes"]["github.label"] == "T-7"


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

    assert recorded["event_type"] == "github.pull_request"
    assert recorded["payload"] == {
        "action": "opened",
        "pull_request": {
            "label": "#403",
            "title": "Fix trace ticket chip",
            "url": PR_URL,
            "merged": False,
        },
    }
    assert recorded["attributes"]["github.label"] == "#403"


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
        "event_type": "github.issue",
        "payload": {
            "action": "opened",
            "issue": {"label": "T-7", "title": "Follow-up", "url": TICKET_URL},
        },
        "attributes": {
            "github.action": "opened",
            "github.kind": "ticket",
            "github.label": "T-7",
            "github.url": TICKET_URL,
            "github.title": "Follow-up",
        },
        "default_source": "github",
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

    assert recorded["event_type"] == "github.issue"
    assert recorded["payload"] == {
        "action": "closed",
        "issue": {"label": "T-7", "title": "Capability gap", "url": TICKET_URL},
    }
    assert recorded["attributes"]["github.action"] == "closed"
    assert recorded["attributes"]["github.label"] == "T-7"
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

    assert recorded["event_type"] == "github.issue_comment"
    assert recorded["payload"] == {
        "action": "commented",
        "issue": {"label": "#42", "title": "", "url": ticket_url},
        "comment": {
            "id": "issuecomment-123",
            "url": f"{ticket_url}#issuecomment-123",
        },
    }
    assert recorded["attributes"] == {
        "github.action": "commented",
        "github.kind": "ticket",
        "github.label": "#42",
        "github.url": ticket_url,
        "github.title": "",
    }
    assert recorded["person_id"] == "aiko"
