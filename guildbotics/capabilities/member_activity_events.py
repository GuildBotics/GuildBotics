from __future__ import annotations

from typing import Any

from guildbotics.entities.team import Person
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.observability.event_types import GITHUB_WORK_TARGET_EVENT_TYPE

#: What a work target's kind is called in an event's type and payload.
EVENT_SUBJECTS = {"ticket": "issue", "pull_request": "pull_request"}


def target_record(target: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The work target ``target`` as an event's payload entry and attributes:
    its URL, and the label its service shows it by."""
    entry = {
        "label": str(target.get("label") or ""),
        "title": str(target.get("title") or "").strip(),
        "url": str(target.get("url") or ""),
    }
    attributes = {
        "github.kind": str(target.get("kind") or ""),
        "github.label": entry["label"],
        "github.url": entry["url"],
        "github.title": entry["title"],
    }
    return entry, attributes


def record_member_work_target(
    member_person: Person, target: dict[str, Any], *, read_only: bool
) -> None:
    """Declare the ticket / PR a member command worked on, or only read.

    The trace the command runs inside names its first recorded target
    (``observability.trace_title``). A read declares one as well, marked
    ``inspected``, so diagnostics learn the target without the read becoming
    activity of its own.
    """
    entry, attributes = target_record(target)
    _record_member_domain_event(
        member_person,
        GITHUB_WORK_TARGET_EVENT_TYPE,
        {EVENT_SUBJECTS.get(str(target.get("kind")), ""): entry},
        {"github.action": "inspected" if read_only else "", **attributes},
    )


def record_member_push_event(member_person: Person, payload: dict[str, Any]) -> None:
    if not payload.get("pushed"):
        return
    branch = str(payload.get("branch") or "")
    _record_member_domain_event(
        member_person,
        "github.push",
        {
            "action": "push",
            "ref": f"refs/heads/{branch}" if branch else "",
            "commits": payload.get("commits", []),
        },
        {"github.action": "push"},
    )


def record_member_pr_create_event(
    member_person: Person, payload: dict[str, Any]
) -> None:
    if payload.get("created"):
        _record_target_event(member_person, "opened", payload["target"], merged=False)


def record_member_issue_create_event(
    member_person: Person, payload: dict[str, Any]
) -> None:
    _record_target_event(member_person, "opened", payload["target"])


def record_member_issue_close_event(
    member_person: Person, payload: dict[str, Any]
) -> None:
    """Record a ticket close as member activity.

    Only a transition into the closed state is recorded: ``state_changed`` in
    the capability result separates a real close from an edit to, or a
    re-close of, an already-closed ticket.

    Only closing is recorded. Reopening is a correction that the work following
    it makes visible again, and the activity history has no reopen event kind
    it could be shown as.
    """
    if str(payload.get("state") or "") == "closed" and payload.get("state_changed"):
        _record_target_event(member_person, "closed", payload["target"])


def record_member_issue_comment_event(
    member_person: Person, payload: dict[str, Any]
) -> None:
    entry, attributes = target_record(payload["target"])
    _record_member_domain_event(
        member_person,
        "github.issue_comment",
        {
            "action": "commented",
            "issue": entry,
            "comment": {"id": payload.get("id"), "url": payload.get("url") or ""},
        },
        {"github.action": "commented", **attributes},
    )


def _record_target_event(
    member_person: Person, action: str, target: dict[str, Any], **fields: Any
) -> None:
    entry, attributes = target_record(target)
    subject = EVENT_SUBJECTS[str(target["kind"])]
    _record_member_domain_event(
        member_person,
        f"github.{subject}",
        {"action": action, subject: {**entry, **fields}},
        {"github.action": action, **attributes},
    )


def _record_member_domain_event(
    member_person: Person,
    event_type: str,
    payload: dict[str, Any],
    attributes: dict[str, Any],
) -> None:
    record_correlated_event(
        event_type=event_type,
        payload=payload,
        attributes=attributes,
        default_source="github",
        person_id=member_person.person_id,
    )
