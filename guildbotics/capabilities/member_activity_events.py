from __future__ import annotations

from typing import Any

from guildbotics.entities.team import Person
from guildbotics.observability.diagnostics_events import record_correlated_event


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
    member_person: Person, repo: str, title: str, payload: dict[str, Any]
) -> None:
    if not payload.get("created"):
        return
    number = payload.get("pr_number")
    url = str(payload.get("pr_url") or "")
    _record_member_domain_event(
        member_person,
        "github.pull_request",
        {
            "action": "opened",
            "pull_request": {
                "number": number,
                "title": title.strip(),
                "html_url": url,
                "merged": False,
            },
        },
        {
            "github.action": "opened",
            "github.kind": "pull_request",
            "github.number": number,
            "github.repo": repo,
            "github.url": url,
        },
    )


def record_member_issue_create_event(
    member_person: Person, payload: dict[str, Any]
) -> None:
    number = payload.get("issue_number")
    title = str(payload.get("issue_title") or "")
    repo = str(payload.get("repo") or "")
    url = str(payload.get("issue_url") or "")
    _record_member_domain_event(
        member_person,
        "github.issue",
        {
            "action": "opened",
            "issue": {
                "number": number,
                "title": title,
                "html_url": url,
            },
        },
        {
            "github.action": "opened",
            "github.kind": "issue",
            "github.number": number,
            "github.repo": repo,
            "github.url": url,
        },
    )


def record_member_issue_comment_event(
    member_person: Person, payload: dict[str, Any]
) -> None:
    number = payload.get("issue_number")
    repo = str(payload.get("repo") or "")
    issue_url = str(payload.get("issue_url") or "")
    comment_url = str(payload.get("comment_url") or payload.get("html_url") or "")
    _record_member_domain_event(
        member_person,
        "github.issue_comment",
        {
            "action": "commented",
            "issue": {
                "number": number,
                "html_url": issue_url,
            },
            "comment": {
                "id": payload.get("comment_id"),
                "html_url": comment_url,
            },
        },
        {
            "github.action": "commented",
            "github.kind": "issue",
            "github.number": number,
            "github.repo": repo,
            "github.url": issue_url,
        },
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
