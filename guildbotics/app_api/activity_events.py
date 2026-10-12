from __future__ import annotations

from typing import Any, Literal

from guildbotics.observability.event_types import SYNC_UPDATE_REJECTED

type ActivityEventType = Literal[
    "pr_create",
    "pr_merge",
    "pr_closed",
    "push",
    "issue_create",
    "issue_resolve",
    "sync_rejected",
    "external",
]


def classify_event(
    event_type: str, payload: dict[str, Any], attributes: dict[str, Any]
) -> ActivityEventType | None:
    normalized_type = event_type.lower().replace("-", "_")
    action = str(payload.get("action") or attributes.get("github.action") or "").lower()
    github_kind = str(attributes.get("github.kind") or "").lower()
    pull_request = payload.get("pull_request")
    issue = payload.get("issue")

    if event_type == SYNC_UPDATE_REJECTED:
        return "sync_rejected"
    if "push" in normalized_type or "commits" in payload or action == "push":
        return "push"
    if "merge" in normalized_type or _payload_bool(pull_request, "merged"):
        return "pr_merge"
    if (
        pull_request is not None
        or github_kind == "pull_request"
        or "pull_request" in normalized_type
    ):
        if (
            action in {"opened", "created", "ready_for_review"}
            or "create" in normalized_type
        ):
            return "pr_create"
        if action == "closed" or "closed" in normalized_type:
            return "pr_closed"
    if issue is not None or github_kind == "ticket" or "issue" in normalized_type:
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
    if classification == "sync_rejected":
        paths = rejected_paths(payload)
        return (
            f"Update not applied: {len(paths)} file(s)"
            if paths
            else "Update not applied"
        )
    if classification == "push":
        commits = commit_entries(payload)
        if len(commits) == 1:
            return commit_message(commits[0]) or "Push: 1 commit"
        return f"Push: {len(commits)} commits" if commits else "Push"
    prefix = {
        "pr_create": "PR",
        "pr_merge": "PR",
        "pr_closed": "PR",
        "issue_create": "Ticket",
        "issue_resolve": "Ticket",
        "external": "Event",
    }[classification]
    suffix = {
        "pr_create": "Created",
        "pr_merge": "Merged",
        "pr_closed": "Closed",
        "issue_create": "Created",
        "issue_resolve": "Resolved",
        "external": "",
    }[classification]
    return " ".join(part for part in (prefix, label, suffix) if part)


def rejected_paths(payload: dict[str, Any]) -> list[str]:
    """Return the shared paths a rejected local change covered."""
    paths = payload.get("paths")
    return [str(path) for path in paths] if isinstance(paths, list) else []


def event_detail(
    item: dict[str, Any], payload: dict[str, Any], attributes: dict[str, Any]
) -> str:
    for value in (
        payload.get("title"),
        _dict_value(payload.get("pull_request"), "title"),
        _dict_value(payload.get("issue"), "title"),
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
        attributes.get("github.url"),
        _dict_value(payload.get("pull_request"), "url"),
        _dict_value(payload.get("issue"), "url"),
        payload.get("html_url"),
        payload.get("compare"),
    ):
        if value:
            return str(value)
    return fallback_url


def target_label(payload: dict[str, Any], attributes: dict[str, Any]) -> str:
    """The label the work target's service shows it by."""
    for value in (
        attributes.get("github.label"),
        _dict_value(payload.get("pull_request"), "label"),
        _dict_value(payload.get("issue"), "label"),
    ):
        if value:
            return str(value)
    return ""


def github_attrs_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    pull_request = payload.get("pull_request")
    issue = payload.get("issue")
    if isinstance(pull_request, dict):
        attrs["github.kind"] = "pull_request"
        attrs["github.label"] = pull_request.get("label")
        attrs["github.url"] = pull_request.get("url")
    elif isinstance(issue, dict):
        attrs["github.kind"] = "ticket"
        attrs["github.label"] = issue.get("label")
        attrs["github.url"] = issue.get("url")
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
