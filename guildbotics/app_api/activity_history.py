from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, cast

from guildbotics.app_api.activity_events import (
    ActivityEventType,
    classify_event,
    event_detail,
    event_label,
    event_url,
    github_attrs_from_payload,
)
from guildbotics.app_api.activity_links import links_from_record, links_from_records
from guildbotics.app_api.models import (
    ActivityHistoryEvent,
    ActivityHistoryLink,
    ActivityHistoryMember,
    ActivityHistoryResponse,
    ActivityHistorySession,
)
from guildbotics.entities.team import Person
from guildbotics.utils.timestamps import parse_iso_datetime


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
    links = links_from_records(records, attributes)
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
    payload = (
        cast(dict[str, Any], item.get("payload"))
        if isinstance(item.get("payload"), dict)
        else {}
    )
    attributes = (
        cast(dict[str, Any], item.get("attributes"))
        if isinstance(item.get("attributes"), dict)
        else {}
    )
    classification = classify_event(event_type, payload, attributes)
    if classification is None:
        return None
    person_id = str(item.get("person_id") or "")
    if person_id and person_id not in display_member_ids:
        return None
    link_attrs = dict(attributes)
    link_attrs.update(github_attrs_from_payload(payload))
    links = links_from_record(payload, link_attrs)
    url = event_url(payload, attributes, links[0].url if links else "")
    label = event_label(payload, attributes, classification)
    return ActivityHistoryEvent(
        id=_event_id(item, index, classification, url),
        timestamp=timestamp,
        person_id=person_id,
        type=classification,
        title=label,
        detail=event_detail(item, payload, attributes),
        url=url,
        links=links,
    )


def _event_id(
    item: dict[str, Any], index: int, classification: ActivityEventType, url: str
) -> str:
    trace_id = str(item.get("trace_id") or "global")
    timestamp = str(item.get("timestamp") or "")
    return f"{trace_id}:{timestamp}:{classification}:{url or index}"


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
