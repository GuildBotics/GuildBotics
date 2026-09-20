from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, cast

from guildbotics.app_api.activity_events import (
    ActivityEventType,
    classify_event,
    event_detail,
    event_label,
    event_url,
    github_attrs_from_payload,
    rejected_paths,
)
from guildbotics.app_api.activity_links import links_from_record, links_from_records
from guildbotics.app_api.models import (
    ActivityHistoryEvent,
    ActivityHistoryLink,
    ActivityHistoryMember,
    ActivityHistoryRateLimit,
    ActivityHistoryRejection,
    ActivityHistoryResponse,
    ActivityHistorySession,
)
from guildbotics.entities.team import Person
from guildbotics.observability.trace_status import resolve_trace_status
from guildbotics.observability.trace_title import (
    CompletionSummary,
    first_seen_attributes,
    is_read_only_record,
    resolve_trace_title,
)
from guildbotics.utils.timestamps import parse_iso_datetime

type ActivitySessionMode = Literal["interactive", "workflow"]
AUTOMATED_WORKFLOW_SOURCES = {"routine", "scheduled", "event_listener"}
# Desktop command runs (commands page and hotkey quick run) fire far too often
# to belong on the activity timeline, so they never become sessions. Whatever
# such a run actually changed still surfaces through activity events.
MANUAL_SESSION_SOURCE = "manual"


def build_activity_history(
    *,
    start: datetime,
    end: datetime,
    members: Iterable[Person],
    records: Iterable[dict[str, Any]],
    completion_summary: CompletionSummary | None = None,
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
    ordered_records = sorted(records, key=_record_sort_key)
    sessions = _build_sessions(
        ordered_records,
        display_member_ids,
        completion_summary or (lambda _attributes, _person_id: ""),
    )
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
    records: list[dict[str, Any]],
    display_member_ids: set[str],
    completion_summary: CompletionSummary,
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
        summary = _summarize_trace(trace_id, trace_records, completion_summary)
        if summary is None:
            continue
        sessions.append(summary)
    sessions.sort(key=lambda session: _timestamp_sort_key(session.started_at))
    return sessions


def _summarize_trace(
    trace_id: str,
    records: list[dict[str, Any]],
    completion_summary: CompletionSummary,
) -> ActivityHistorySession | None:
    timestamps = [
        parsed
        for item in records
        if (parsed := parse_timestamp(str(item.get("timestamp", "")))) is not None
    ]
    if not timestamps:
        return None
    source = _first_text(records, "source")
    if source == MANUAL_SESSION_SOURCE:
        return None
    first = records[0]
    attributes = first_seen_attributes(records)
    command = _first_text(records, "command")
    workflow = _first_text(records, "workflow")
    status = resolve_trace_status(records)
    started_at = min(timestamps)
    ended_at = max(timestamps)
    # A read (PR / issue inspect, memory recall) names what the session looked
    # at, which titles it, but is not its work, so it yields no link.
    worked = [item for item in records if not is_read_only_record(item)]
    links = links_from_records(worked, first_seen_attributes(worked))
    rate_limit = _rate_limit_from_records(records)
    mode: ActivitySessionMode = "interactive" if source == "interactive" else "workflow"
    if (
        mode == "workflow"
        and source in AUTOMATED_WORKFLOW_SOURCES
        and rate_limit is None
        and not _has_workflow_activity_evidence(records, attributes, links)
    ):
        return None
    person_id = str(first.get("person_id") or "")
    return ActivityHistorySession(
        trace_id=trace_id,
        person_id=person_id,
        source=source,
        command=command,
        workflow=workflow,
        title=resolve_trace_title(
            records,
            attributes,
            person_id=person_id,
            command=command,
            workflow=workflow,
            completion_summary=completion_summary,
            fallback=trace_id,
        ),
        mode=mode,
        status=status,
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        duration_seconds=max(0.0, (ended_at - started_at).total_seconds()),
        links=links,
        rate_limit=rate_limit,
    )


def _has_workflow_activity_evidence(
    records: list[dict[str, Any]],
    attributes: dict[str, Any],
    links: list[ActivityHistoryLink],
) -> bool:
    if links:
        return True
    if _has_work_target_attributes(attributes):
        return True
    return any(_record_indicates_work(item) for item in records)


def _has_work_target_attributes(attributes: dict[str, Any]) -> bool:
    return any(
        _attribute_has_value(attributes, key)
        for key in (
            "github.number",
            "github.url",
            "github.kind",
            "memory.doc_id",
            "memory.path",
            "memory.title",
            "memory.action",
        )
    )


def _attribute_has_value(attributes: dict[str, Any], key: str) -> bool:
    value = attributes.get(key)
    return value is not None and str(value).strip() != ""


def _record_indicates_work(item: dict[str, Any]) -> bool:
    kind = str(item.get("kind") or "")
    if kind == "memory":
        return True
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return False
    return bool(
        _first_payload_value(payload, "title", "prompt", "response", "stdout")
        or _source_payload_has_url(payload)
    )


def _first_payload_value(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    fields = payload.get("fields")
    if isinstance(fields, dict):
        for key in keys:
            value = fields.get(key)
            if value:
                return str(value)
    return ""


def _source_payload_has_url(payload: dict[str, Any]) -> bool:
    source = payload.get("source")
    return isinstance(source, list) and any(
        isinstance(item, dict) and item.get("url") for item in source
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
    events.sort(key=lambda event: _timestamp_sort_key(event.timestamp))
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
    rejection = None
    if classification == "sync_rejected":
        rejection = _rejection(payload)
        if rejection is None:
            return None
    link_attrs = dict(attributes)
    link_attrs.update(github_attrs_from_payload(payload))
    links = links_from_record(payload, link_attrs, item)
    url = event_url(payload, attributes, links[0].url if links else "")
    label = event_label(payload, attributes, classification)
    return ActivityHistoryEvent(
        id=_event_id(item, index, classification, url),
        timestamp=timestamp,
        person_id=person_id,
        type=classification,
        title=label,
        detail=(
            ", ".join(rejection.paths)
            if rejection is not None
            else event_detail(item, payload, attributes)
        ),
        url=url,
        links=links,
        rejection=rejection,
    )


def _rejection(payload: dict[str, Any]) -> ActivityHistoryRejection | None:
    """Describe a rejected local change, or nothing when it cannot be found again.

    A record without a ``rejection_id`` names no stashed commit, so it would
    tell the user something happened without telling them where to look.
    """
    rejection_id = str(payload.get("rejection_id") or "")
    if not rejection_id:
        return None
    return ActivityHistoryRejection(
        rejection_id=rejection_id,
        paths=rejected_paths(payload),
        source_device_id=str(payload.get("source_device_id") or ""),
    )


def _event_id(
    item: dict[str, Any], index: int, classification: ActivityEventType, url: str
) -> str:
    trace_id = str(item.get("trace_id") or "global")
    timestamp = str(item.get("timestamp") or "")
    return f"{trace_id}:{timestamp}:{classification}:{url or index}"


def _rate_limit_from_records(
    records: list[dict[str, Any]],
) -> ActivityHistoryRateLimit | None:
    latest: dict[str, Any] | None = None
    for item in records:
        if str(item.get("type") or "") != "workflow.rate_limited":
            continue
        if latest is None or _record_sort_key(latest) <= _record_sort_key(item):
            latest = item
    if latest is None:
        return None
    attributes = latest.get("attributes")
    payload = latest.get("payload")
    attr_data = attributes if isinstance(attributes, dict) else {}
    payload_data = payload if isinstance(payload, dict) else {}
    return ActivityHistoryRateLimit(
        retry_after_at=str(
            attr_data.get("rate_limit.retry_after_at")
            or payload_data.get("retry_after_at")
            or ""
        ),
        retry_after_text=str(
            attr_data.get("rate_limit.retry_after_text")
            or payload_data.get("retry_after_text")
            or ""
        ),
    )


def _first_text(records: list[dict[str, Any]], key: str) -> str:
    for item in records:
        value = item.get(key)
        if value:
            return str(value)
    return ""


def run_subject_id(attributes: Mapping[str, Any]) -> str:
    """Reconstruct the run subject id from trace attributes.

    Mirrors the ``subject_id`` a workflow records on completion so a trace can
    be joined to its completion summary. Chat traces key on the
    provider/channel/thread/event tuple; ticket traces key on the GitHub url.
    """
    provider = str(attributes.get("event.provider") or "").strip()
    if provider:
        channel = str(attributes.get("slack.channel") or "")
        thread_ts = str(attributes.get("slack.thread_ts") or "")
        event_id = str(attributes.get("event_id") or "")
        return f"{provider}:{channel}:{thread_ts}:{event_id}"
    return str(attributes.get("github.url") or "")


def parse_timestamp(value: str) -> datetime | None:
    return parse_iso_datetime(value)


def _record_sort_key(item: dict[str, Any]) -> datetime:
    return _timestamp_sort_key(str(item.get("timestamp") or ""))


def _timestamp_sort_key(value: str) -> datetime:
    parsed = parse_timestamp(value)
    return (
        parsed.astimezone(UTC)
        if parsed is not None
        else datetime.min.replace(tzinfo=UTC)
    )
