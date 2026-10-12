from __future__ import annotations

from typing import Any

from guildbotics.entities.team import Person
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.observability.event_types import TARGET_RECORDED_EVENT_TYPE


def target_record(target: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The work target ``target`` as an event's payload entry and attributes:
    its URL, and the label its service shows it by."""
    entry = {
        "label": str(target.get("label") or ""),
        "title": str(target.get("title") or "").strip(),
        "url": str(target.get("url") or ""),
    }
    attributes = {
        "target.provider": str(target.get("provider") or ""),
        "target.kind": str(target.get("kind") or ""),
        "target.label": entry["label"],
        "target.url": entry["url"],
        "target.title": entry["title"],
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
        TARGET_RECORDED_EVENT_TYPE,
        {str(target["kind"]): entry},
        {"target.action": "inspected" if read_only else "", **attributes},
    )


def record_member_push_event(
    member_person: Person, payload: dict[str, Any], *, provider: str
) -> None:
    if not payload.get("pushed"):
        return
    branch = str(payload.get("branch") or "")
    _record_member_domain_event(
        member_person,
        "repository.push",
        {
            "action": "push",
            "ref": f"refs/heads/{branch}" if branch else "",
            "commits": payload.get("commits", []),
        },
        {"target.action": "push", "target.provider": provider},
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
        "ticket.comment",
        {
            "action": "commented",
            "ticket": entry,
            "comment": {"id": payload.get("id"), "url": payload.get("url") or ""},
        },
        {"target.action": "commented", **attributes},
    )


def _record_target_event(
    member_person: Person, action: str, target: dict[str, Any], **fields: Any
) -> None:
    entry, attributes = target_record(target)
    subject = str(target["kind"])
    _record_member_domain_event(
        member_person,
        f"{subject}.item",
        {"action": action, subject: {**entry, **fields}},
        {"target.action": action, **attributes},
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
        default_source=str(attributes.get("target.provider") or ""),
        person_id=member_person.person_id,
    )
