from __future__ import annotations

from typing import Any

from guildbotics.app_api.diagnostics_events import record_correlated_event


def record_member_push_event(member_person: Any, payload: dict[str, Any]) -> None:
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
    member_person: Any, repo: str, title: str, payload: dict[str, Any]
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


def _record_member_domain_event(
    member_person: Any,
    event_type: str,
    payload: dict[str, Any],
    attributes: dict[str, Any],
) -> None:
    record_correlated_event(
        event_type=event_type,
        payload=payload,
        attributes=attributes,
        default_source="github",
        person_id=str(getattr(member_person, "person_id", "")),
    )
