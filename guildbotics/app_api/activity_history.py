from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Literal, cast

from guildbotics.app_api.activity_events import (
    ActivityEventType,
    classify_event,
    event_detail,
    event_label,
    event_url,
    github_attrs_from_payload,
)
from guildbotics.app_api.activity_links import (
    MEMORY_READ_ONLY_ACTIONS,
    links_from_record,
    links_from_records,
)
from guildbotics.app_api.models import (
    ActivityHistoryEvent,
    ActivityHistoryLink,
    ActivityHistoryMember,
    ActivityHistoryRateLimit,
    ActivityHistoryResponse,
    ActivityHistorySession,
)
from guildbotics.entities.team import Person
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.timestamps import parse_iso_datetime

type ActivitySessionMode = Literal["interactive", "workflow"]
AUTOMATED_WORKFLOW_SOURCES = {"routine", "scheduled", "event_listener"}
# Internal grouping key: the trace that owns a run-scoped record. Kept separate
# from the record's own ``trace_id`` so adopted records still generate links
# (e.g. memory diagnostics urls) against their original identity.
_OWNER_TRACE_KEY = "_owner_trace_id"


def build_activity_history(
    *,
    start: datetime,
    end: datetime,
    members: Iterable[Person],
    records: Iterable[dict[str, Any]],
    run_summary: Callable[[str, str], str] | None = None,
    run_subject: Callable[[str], str] | None = None,
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
    record_list = _attach_run_scoped_records(
        list(records), run_subject or (lambda _run_id: "")
    )
    ordered_records = sorted(record_list, key=_record_sort_key)
    sessions = _build_sessions(
        ordered_records,
        display_member_ids,
        run_summary or (lambda _subject_id, _person_id: ""),
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
    run_summary: Callable[[str, str], str],
) -> list[ActivityHistorySession]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        trace_id = str(item.get(_OWNER_TRACE_KEY) or item.get("trace_id") or "")
        person_id = str(item.get("person_id") or "")
        if not trace_id or person_id not in display_member_ids:
            continue
        grouped.setdefault(trace_id, []).append(item)

    sessions: list[ActivityHistorySession] = []
    for trace_id, trace_records in grouped.items():
        summary = _summarize_trace(trace_id, trace_records, run_summary)
        if summary is None:
            continue
        sessions.append(summary)
    sessions.sort(key=lambda session: _timestamp_sort_key(session.started_at))
    return sessions


def _summarize_trace(
    trace_id: str,
    records: list[dict[str, Any]],
    run_summary: Callable[[str, str], str],
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
    rate_limit = _rate_limit_from_records(records)
    if rate_limit is not None:
        status = "rate_limited"
    mode: ActivitySessionMode = "interactive" if source == "interactive" else "workflow"
    if (
        mode == "workflow"
        and source in AUTOMATED_WORKFLOW_SOURCES
        and rate_limit is None
        and not _has_workflow_activity_evidence(records, attributes, links)
    ):
        return None
    person_id = str(first.get("person_id") or "")
    completion_summary = _first_line(run_summary(_subject_id(attributes), person_id))
    return ActivityHistorySession(
        trace_id=trace_id,
        person_id=person_id,
        source=source,
        command=command,
        workflow=workflow,
        title=_session_title(
            trace_id, records, command, workflow, links, attributes, completion_summary
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
        if kind == "event" and event_type == "workflow.rate_limited":
            return "rate_limited"
        if kind == "event" and event_type.endswith(".failed"):
            return "failed"
        if kind == "log" and level in {"ERROR", "CRITICAL"}:
            return "failed"
        if kind == "event" and event_type.endswith(".started") and status == "info":
            status = "running"
        if kind == "event" and event_type.endswith(".finished"):
            status = "success"
    return status


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
    attributes: dict[str, Any],
    completion_summary: str,
) -> str:
    for value in (
        completion_summary,
        attributes.get("memory.title"),
        _first_payload_text(records, "title"),
        _first_work_link_label(links),
        _trigger_label(attributes),
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


def _attach_run_scoped_records(
    records: list[dict[str, Any]], run_subject: Callable[[str], str]
) -> list[dict[str, Any]]:
    """Adopt orphan run-scoped records into the trace that owns their subject.

    A workflow subprocess (chat reply, memory write) records without the parent
    trace id but tags each record with a run id (``run_id`` for chat workflows,
    ``task_run_id`` for ticket workflows). Mapping that run to its ``subject_id``
    and back to the trace that reconstructs the same subject lets those records
    (and their memory/doc links) show up on the originating session instead of
    vanishing.

    Returns a new list; adopted records are shallow-copied with the owner key so
    the caller's record dicts are never mutated (safe to reuse the input array).
    """
    trace_by_key: dict[tuple[str, str], str] = {}
    for item in records:
        trace_id = str(item.get("trace_id") or "")
        attributes = item.get("attributes")
        if not trace_id or not isinstance(attributes, dict):
            continue
        subject = _subject_id(attributes)
        if subject:
            person_id = str(item.get("person_id") or "")
            trace_by_key.setdefault((subject, person_id), trace_id)

    adopted: list[dict[str, Any]] = []
    for item in records:
        owner_trace = _run_scoped_owner_trace(item, run_subject, trace_by_key)
        adopted.append({**item, _OWNER_TRACE_KEY: owner_trace} if owner_trace else item)
    return adopted


def _run_scoped_owner_trace(
    item: dict[str, Any],
    run_subject: Callable[[str], str],
    trace_by_key: dict[tuple[str, str], str],
) -> str:
    attributes = item.get("attributes")
    if item.get("trace_id") or not isinstance(attributes, dict):
        return ""
    if _is_read_only_memory(item):
        return ""
    run_id = str(attributes.get("run_id") or attributes.get("task_run_id") or "")
    if not run_id:
        return ""
    person_id = str(item.get("person_id") or "")
    return trace_by_key.get((run_subject(run_id), person_id), "")


def _subject_id(attributes: dict[str, Any]) -> str:
    """Reconstruct the run subject id from trace attributes.

    Mirrors the ``subject_id`` a workflow records on completion so activity
    history can join a trace to its completion summary. Chat traces key on the
    provider/channel/thread/event tuple; ticket traces key on the GitHub url.
    """
    provider = str(attributes.get("event.provider") or "").strip()
    if provider:
        channel = str(attributes.get("slack.channel") or "")
        thread_ts = str(attributes.get("slack.thread_ts") or "")
        event_id = str(attributes.get("event_id") or "")
        return f"{provider}:{channel}:{thread_ts}:{event_id}"
    return str(attributes.get("github.url") or "")


def _trigger_label(attributes: dict[str, Any]) -> str:
    """Build a provider-neutral label for an event-triggered session.

    ``event.provider`` is only present on chat-triggered workflows, so its
    presence is what selects this label. It gives an in-progress chat session
    a meaningful title before its completion summary exists, instead of
    falling through to the raw agent prompt.
    """
    provider = str(attributes.get("event.provider") or "").strip()
    if not provider:
        return ""
    return t("app_api.activity_history.chat_trigger", provider=provider.title())


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _first_work_link_label(links: list[ActivityHistoryLink]) -> str:
    for kind in ("pull_request", "issue", "doc"):
        for link in links:
            if link.kind == kind and link.label:
                return link.label
    return ""


def _is_read_only_memory(item: dict[str, Any]) -> bool:
    """True for memory reads/signals (recall/get/touch) that change nothing.

    Their payload title is generic ("Memory recall") or just the doc that was
    read, so they must not become a session title or an activity link.
    """
    attributes = item.get("attributes")
    return (
        isinstance(attributes, dict)
        and str(attributes.get("memory.action") or "") in MEMORY_READ_ONLY_ACTIONS
    )


def _first_payload_text(records: list[dict[str, Any]], key: str) -> str:
    for item in records:
        payload = item.get("payload")
        if not isinstance(payload, dict) or _is_read_only_memory(item):
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


def _record_sort_key(item: dict[str, Any]) -> datetime:
    return _timestamp_sort_key(str(item.get("timestamp") or ""))


def _timestamp_sort_key(value: str) -> datetime:
    parsed = parse_timestamp(value)
    return (
        parsed.astimezone(UTC)
        if parsed is not None
        else datetime.min.replace(tzinfo=UTC)
    )
