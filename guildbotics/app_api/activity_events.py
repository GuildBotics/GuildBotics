from __future__ import annotations

from typing import Any, Literal

type ActivityEventType = Literal[
    "pr_create", "pr_merge", "pr_closed", "push", "issue_resolve", "external"
]


def classify_event(
    event_type: str, payload: dict[str, Any], attributes: dict[str, Any]
) -> ActivityEventType | None:
    normalized_type = event_type.lower().replace("-", "_")
    action = str(payload.get("action") or attributes.get("github.action") or "").lower()
    github_kind = str(attributes.get("github.kind") or "").lower()
    pull_request = payload.get("pull_request")
    issue = payload.get("issue")

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
    if (issue is not None or github_kind == "issue" or "issue" in normalized_type) and (
        action in {"closed", "resolved"} or "resolve" in normalized_type
    ):
        return "issue_resolve"
    return None


def event_label(
    payload: dict[str, Any],
    attributes: dict[str, Any],
    classification: ActivityEventType,
) -> str:
    number = github_number(payload, attributes)
    if classification == "push":
        commits = commit_entries(payload)
        if len(commits) == 1:
            return commit_message(commits[0]) or "Push: 1 commit"
        return f"Push: {len(commits)} commits" if commits else "Push"
    prefix = {
        "pr_create": "PR",
        "pr_merge": "PR",
        "pr_closed": "PR",
        "issue_resolve": "Issue",
        "external": "Event",
    }[classification]
    suffix = {
        "pr_create": "Created",
        "pr_merge": "Merged",
        "pr_closed": "Closed",
        "issue_resolve": "Resolved",
        "external": "",
    }[classification]
    if number:
        return f"{prefix} #{number} {suffix}".strip()
    return f"{prefix} {suffix}".strip()


def event_detail(
    item: dict[str, Any], payload: dict[str, Any], attributes: dict[str, Any]
) -> str:
    for value in (
        payload.get("title"),
        _dict_value(payload.get("pull_request"), "title"),
        _dict_value(payload.get("issue"), "title"),
        payload.get("ref"),
        item.get("message"),
        attributes.get("github.repo"),
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
        _dict_value(payload.get("pull_request"), "html_url"),
        _dict_value(payload.get("issue"), "html_url"),
        payload.get("html_url"),
        payload.get("compare"),
    ):
        if value:
            return str(value)
    return fallback_url


def github_number(payload: dict[str, Any], attributes: dict[str, Any]) -> str:
    for value in (
        attributes.get("github.number"),
        payload.get("number"),
        _dict_value(payload.get("pull_request"), "number"),
        _dict_value(payload.get("issue"), "number"),
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
        attrs["github.number"] = pull_request.get("number")
        attrs["github.url"] = pull_request.get("html_url")
    elif isinstance(issue, dict):
        attrs["github.kind"] = "issue"
        attrs["github.number"] = issue.get("number")
        attrs["github.url"] = issue.get("html_url")
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
