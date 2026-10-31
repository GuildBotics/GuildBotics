from __future__ import annotations

from typing import Any, Literal

from guildbotics.utils.i18n_tool import t

type ActivityEventType = Literal[
    "pr_create",
    "pr_merge",
    "pr_closed",
    "pr_draft",
    "push",
    "issue_create",
    "issue_resolve",
    "external",
]


def classify_event(
    event_type: str, payload: dict[str, Any], attributes: dict[str, Any]
) -> ActivityEventType | None:
    normalized_type = event_type.lower().replace("-", "_")
    action = str(payload.get("action") or attributes.get("target.action") or "").lower()
    target_kind = str(attributes.get("target.kind") or "").lower()
    pull_request = payload.get("pull_request")
    ticket = payload.get("ticket")

    if event_type == "pull_request.review_limit":
        return "pr_draft"

    if "push" in normalized_type or "commits" in payload or action == "push":
        return "push"
    if "merge" in normalized_type or _payload_bool(pull_request, "merged"):
        return "pr_merge"
    if (
        pull_request is not None
        or target_kind == "pull_request"
        or "pull_request" in normalized_type
    ):
        if (
            action in {"opened", "created", "ready_for_review"}
            or "create" in normalized_type
        ):
            return "pr_create"
        if action == "closed" or "closed" in normalized_type:
            return "pr_closed"
    if ticket is not None or target_kind == "ticket" or "ticket" in normalized_type:
        if action in {"opened", "created"} or "create" in normalized_type:
            return "issue_create"
        if action in {"closed", "resolved"} or "resolve" in normalized_type:
            return "issue_resolve"
    return None


def event_label(
    payload: dict[str, Any],
    attributes: dict[str, Any],
    classification: ActivityEventType,
) -> str:
    label = target_label(payload, attributes)
    if classification == "push":
        commits = commit_entries(payload)
        if len(commits) == 1:
            return commit_message(commits[0]) or "Push: 1 commit"
        return f"Push: {len(commits)} commits" if commits else "Push"
    prefix = {
        "pr_create": "PR",
        "pr_merge": "PR",
        "pr_closed": "PR",
        "pr_draft": "PR",
        "issue_create": "Ticket",
        "issue_resolve": "Ticket",
        "external": "Event",
    }[classification]
    suffix = {
        "pr_create": "Created",
        "pr_merge": "Merged",
        "pr_closed": "Closed",
        "pr_draft": t("app_api.activity_events.review_limit"),
        "issue_create": "Created",
        "issue_resolve": "Resolved",
        "external": "",
    }[classification]
    return " ".join(part for part in (prefix, label, suffix) if part)


def event_detail(
    item: dict[str, Any], payload: dict[str, Any], attributes: dict[str, Any]
) -> str:
    for value in (
        payload.get("title"),
        _dict_value(payload.get("pull_request"), "title"),
        _dict_value(payload.get("ticket"), "title"),
        payload.get("ref"),
        item.get("message"),
    ):
        if value:
            return str(value)
    return str(item.get("type") or "")


def event_url(
    payload: dict[str, Any],
    attributes: dict[str, Any],
    fallback_url: str,
) -> str:
    for value in (
        attributes.get("target.url"),
        _dict_value(payload.get("pull_request"), "url"),
        _dict_value(payload.get("ticket"), "url"),
        payload.get("html_url"),
        payload.get("compare"),
    ):
        if value:
            return str(value)
    return fallback_url


def target_label(payload: dict[str, Any], attributes: dict[str, Any]) -> str:
    """The label the work target's service shows it by."""
    for value in (
        attributes.get("target.label"),
        _dict_value(payload.get("pull_request"), "label"),
        _dict_value(payload.get("ticket"), "label"),
    ):
        if value:
            return str(value)
    return ""


def target_attrs_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    pull_request = payload.get("pull_request")
    ticket = payload.get("ticket")
    if isinstance(pull_request, dict):
        attrs["target.kind"] = "pull_request"
        attrs["target.label"] = pull_request.get("label")
        attrs["target.url"] = pull_request.get("url")
    elif isinstance(ticket, dict):
        attrs["target.kind"] = "ticket"
        attrs["target.label"] = ticket.get("label")
        attrs["target.url"] = ticket.get("url")
    return {key: value for key, value in attrs.items() if value}


def commit_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    commits = payload.get("commits")
    if not isinstance(commits, list):
        return []
    return [commit for commit in commits if isinstance(commit, dict)]


def commit_message(commit: dict[str, Any]) -> str:
    message = str(commit.get("message") or "").strip()
    return message.splitlines()[0] if message else ""


def _payload_bool(value: Any, key: str) -> bool:
    return isinstance(value, dict) and bool(value.get(key))


def _dict_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None
