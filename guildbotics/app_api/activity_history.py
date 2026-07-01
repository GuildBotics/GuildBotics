from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal, cast

from guildbotics.app_api.models import (
    ActivityHistoryEvent,
    ActivityHistoryLink,
    ActivityHistoryMember,
    ActivityHistoryResponse,
    ActivityHistorySession,
)
from guildbotics.entities.team import Person
from guildbotics.utils.timestamps import parse_iso_datetime

type ActivityEventType = Literal[
    "pr_create", "pr_merge", "pr_closed", "push", "issue_resolve", "external"
]
type ActivityLinkKind = Literal["doc", "issue", "pull_request", "commit", "external"]


def build_activity_history(
    *,
    start: datetime,
    end: datetime,
    members: Iterable[Person],
    records: Iterable[dict[str, Any]],
) -> ActivityHistoryResponse:
    display_members = [
        ActivityHistoryMember(
            person_id=member.person_id,
            name=member.name,
            person_type=str(getattr(member, "person_type", "")),
            roles=sorted(member.roles.keys()),
        )
        for member in members
        if str(getattr(member, "person_type", "")) != "human"
    ]
    display_member_ids = {member.person_id for member in display_members}
    ordered_records = sorted(records, key=lambda item: str(item.get("timestamp", "")))
    sessions = _build_sessions(ordered_records, display_member_ids)
    events = _build_events(ordered_records, display_member_ids)
    return ActivityHistoryResponse(
        start=start.isoformat(),
        end=end.isoformat(),
        members=display_members,
        sessions=sessions,
        events=events,
        unsupported_event_sources=[],
    )


def _build_sessions(
    records: list[dict[str, Any]], display_member_ids: set[str]
) -> list[ActivityHistorySession]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        trace_id = str(item.get("trace_id") or "")
        person_id = str(item.get("person_id") or "")
        if not trace_id or person_id not in display_member_ids:
            continue
        grouped.setdefault(trace_id, []).append(item)

    sessions: list[ActivityHistorySession] = []
    for trace_id, trace_records in grouped.items():
        summary = _summarize_trace(trace_id, trace_records)
        if summary is None:
            continue
        sessions.append(summary)
    sessions.sort(key=lambda session: session.started_at)
    return sessions


def _summarize_trace(
    trace_id: str, records: list[dict[str, Any]]
) -> ActivityHistorySession | None:
    timestamps = [
        parsed
        for item in records
        if (parsed := parse_timestamp(str(item.get("timestamp", "")))) is not None
    ]
    if not timestamps:
        return None
    first = records[0]
    attributes = _merged_attributes(records)
    source = _first_text(records, "source")
    command = _first_text(records, "command")
    workflow = _first_text(records, "workflow")
    status = _trace_status(records)
    started_at = min(timestamps)
    ended_at = max(timestamps)
    links = _links_from_records(records, attributes)
    return ActivityHistorySession(
        trace_id=trace_id,
        person_id=str(first.get("person_id") or ""),
        source=source,
        command=command,
        workflow=workflow,
        title=_session_title(trace_id, records, command, workflow, links),
        mode="interactive" if source in {"interactive", "manual"} else "workflow",
        status=status,
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        duration_seconds=max(0.0, (ended_at - started_at).total_seconds()),
        links=links,
    )


def _build_events(
    records: list[dict[str, Any]], display_member_ids: set[str]
) -> list[ActivityHistoryEvent]:
    events: list[ActivityHistoryEvent] = []
    seen: set[str] = set()
    for index, item in enumerate(records):
        event = _activity_event(item, index, display_member_ids)
        if event is None or event.id in seen:
            continue
        seen.add(event.id)
        events.append(event)
    events.sort(key=lambda event: event.timestamp)
    return events


def _activity_event(
    item: dict[str, Any], index: int, display_member_ids: set[str]
) -> ActivityHistoryEvent | None:
    timestamp = str(item.get("timestamp") or "")
    if parse_timestamp(timestamp) is None:
        return None
    event_type = str(item.get("type") or "")
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    payload = cast(dict[str, Any], payload)
    attributes = (
        item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    )
    attributes = cast(dict[str, Any], attributes)
    classification = _classify_event(event_type, payload, attributes)
    if classification is None:
        return None
    person_id = str(item.get("person_id") or "")
    if person_id and person_id not in display_member_ids:
        return None
    link_attrs = dict(attributes)
    link_attrs.update(_github_attrs_from_payload(payload))
    links = _links_from_record(payload, link_attrs)
    url = _event_url(payload, attributes, links)
    label = _event_label(payload, attributes, classification)
    return ActivityHistoryEvent(
        id=_event_id(item, index, classification, url),
        timestamp=timestamp,
        person_id=person_id,
        type=classification,
        title=label,
        detail=_event_detail(item, payload, attributes),
        url=url,
        links=links,
    )


def _classify_event(
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


def _payload_bool(value: Any, key: str) -> bool:
    return isinstance(value, dict) and bool(value.get(key))


def _event_id(
    item: dict[str, Any], index: int, classification: ActivityEventType, url: str
) -> str:
    trace_id = str(item.get("trace_id") or "global")
    timestamp = str(item.get("timestamp") or "")
    return f"{trace_id}:{timestamp}:{classification}:{url or index}"


def _event_label(
    payload: dict[str, Any],
    attributes: dict[str, Any],
    classification: ActivityEventType,
) -> str:
    number = _github_number(payload, attributes)
    if classification == "push":
        commit_count = (
            len(payload.get("commits", []))
            if isinstance(payload.get("commits"), list)
            else 0
        )
        return f"Push: {commit_count} commits" if commit_count else "Push"
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


def _event_detail(
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


def _event_url(
    payload: dict[str, Any],
    attributes: dict[str, Any],
    links: list[ActivityHistoryLink],
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
    return links[0].url if links else ""


def _github_number(payload: dict[str, Any], attributes: dict[str, Any]) -> str:
    for value in (
        attributes.get("github.number"),
        payload.get("number"),
        _dict_value(payload.get("pull_request"), "number"),
        _dict_value(payload.get("issue"), "number"),
    ):
        if value:
            return str(value)
    return ""


def _github_attrs_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
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


def _dict_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


def _trace_status(records: list[dict[str, Any]]) -> str:
    status = "info"
    for item in records:
        kind = item.get("kind")
        event_type = str(item.get("type") or "")
        level = str(item.get("level") or "").upper()
        if kind == "event" and event_type.endswith(".failed"):
            return "failed"
        if kind == "log" and level in {"ERROR", "CRITICAL"}:
            return "failed"
        if kind == "event" and event_type.endswith(".started") and status == "info":
            status = "running"
        if kind == "event" and event_type.endswith(".finished"):
            status = "success"
    return status


def _first_text(records: list[dict[str, Any]], key: str) -> str:
    for item in records:
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _merged_attributes(records: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for item in records:
        attributes = item.get("attributes")
        if isinstance(attributes, dict):
            merged.update(attributes)
    return merged


def _session_title(
    trace_id: str,
    records: list[dict[str, Any]],
    command: str,
    workflow: str,
    links: list[ActivityHistoryLink],
) -> str:
    attributes = _merged_attributes(records)
    for value in (
        attributes.get("memory.title"),
        _first_payload_text(records, "title"),
        _first_work_link_label(links),
        _first_payload_field(records, "prompt"),
        workflow,
        command,
        attributes.get("memory.doc_id"),
        _first_payload_field(records, "brain"),
        _first_payload_field(records, "cli_agent"),
        _first_record_text(records, "type", "event", "message"),
    ):
        if value:
            return str(value)
    return trace_id


def _first_work_link_label(links: list[ActivityHistoryLink]) -> str:
    for kind in ("pull_request", "issue", "doc"):
        for link in links:
            if link.kind == kind and link.label:
                return link.label
    return ""


def _links_from_records(
    records: list[dict[str, Any]], attributes: dict[str, Any]
) -> list[ActivityHistoryLink]:
    links = _links_from_attributes(attributes)
    for item in records:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        payload = cast(dict[str, Any], payload)
        item_attributes = (
            item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        )
        item_attributes = cast(dict[str, Any], item_attributes)
        links.extend(_links_from_record(payload, item_attributes))
    return _dedupe_links(links)


def _links_from_record(
    payload: dict[str, Any], attributes: dict[str, Any]
) -> list[ActivityHistoryLink]:
    links = _links_from_attributes(attributes)
    doc_link = _doc_link(payload, attributes)
    if doc_link is not None:
        links.append(doc_link)
    links.extend(_commit_links(payload))
    return _dedupe_links(links)


def _links_from_attributes(attributes: dict[str, Any]) -> list[ActivityHistoryLink]:
    url = str(attributes.get("github.url") or "")
    number = str(attributes.get("github.number") or "")
    kind = str(attributes.get("github.kind") or "")
    if not (url or number):
        return []
    link_kind: ActivityLinkKind = (
        "pull_request" if kind == "pull_request" or "/pull/" in url else "issue"
    )
    prefix = "PR" if link_kind == "pull_request" else "Issue"
    label = f"{prefix} #{number}" if number else url
    return [ActivityHistoryLink(kind=link_kind, label=label, url=url)]


def _doc_link(
    payload: dict[str, Any], attributes: dict[str, Any]
) -> ActivityHistoryLink | None:
    path = str(attributes.get("memory.path") or "")
    doc_id = str(attributes.get("memory.doc_id") or "")
    title = str(payload.get("title") or "")
    label = title or doc_id or path
    if not label:
        return None
    return ActivityHistoryLink(kind="doc", label=label, url="")


def _commit_links(payload: dict[str, Any]) -> list[ActivityHistoryLink]:
    commits = payload.get("commits")
    if not isinstance(commits, list):
        return []
    links: list[ActivityHistoryLink] = []
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        url = str(commit.get("url") or commit.get("html_url") or "")
        sha = str(commit.get("id") or commit.get("sha") or "")
        if not (url or sha):
            continue
        links.append(
            ActivityHistoryLink(
                kind="commit",
                label=sha[:7] if sha else url,
                url=url,
            )
        )
    return links


def _dedupe_links(
    links: Iterable[ActivityHistoryLink],
) -> list[ActivityHistoryLink]:
    deduped: list[ActivityHistoryLink] = []
    seen: set[tuple[str, str, str]] = set()
    for link in links:
        key = (link.kind, link.label, link.url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(link)
    return deduped


def _first_payload_text(records: list[dict[str, Any]], key: str) -> str:
    for item in records:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _first_payload_field(records: list[dict[str, Any]], key: str) -> str:
    for item in records:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        fields = payload.get("fields")
        if isinstance(fields, dict) and fields.get(key):
            return str(fields[key])
        if payload.get(key):
            return str(payload[key])
    return ""


def _first_record_text(records: list[dict[str, Any]], *keys: str) -> str:
    for item in records:
        for key in keys:
            value = item.get(key)
            if value:
                return str(value)
    return ""


def parse_timestamp(value: str) -> datetime | None:
    return parse_iso_datetime(value)
