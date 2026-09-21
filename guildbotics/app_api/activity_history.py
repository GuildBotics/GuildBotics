"""Build the desktop Activity History from lifecycle records and fact events.

A session on the timeline is one execution (trace). Its existence, times and
status come from the run's one shared lifecycle record -- a task run
(``state/task-runs``) for a workflow, a session record (``state/sessions``)
for an interactive member CLI session -- and its links, title and the layers
above the lifecycle (dispatch decisions, rate limits, completion evidence)
come from the fact events recorded inside the trace. Nothing here folds
command boundary events: those stay on the device that ran the command.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
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
from guildbotics.entities.task_run import TaskRunRecord
from guildbotics.entities.team import Person
from guildbotics.observability.interactive_sessions import INTERACTIVE_SOURCE
from guildbotics.observability.trace_status import TraceStatus
from guildbotics.observability.trace_title import (
    first_seen_attributes,
    is_read_only_record,
    resolve_trace_title,
)
from guildbotics.utils.timestamps import parse_iso_datetime

type ActivitySessionMode = Literal["interactive", "workflow"]
# Desktop command runs (commands page and hotkey quick run) fire far too often
# to belong on the activity timeline, so they never become sessions. Whatever
# such a run actually changed still surfaces through activity events.
MANUAL_SESSION_SOURCE = "manual"
#: An automated run that ended without incident and without touching anything
#: is not activity; every other outcome is, on every device alike. Automated
#: means every workflow run that is not manual, whatever its source says (a
#: record written before the source was recorded says nothing).
_QUIET_STATUSES = frozenset({"success", "running", "info"})


@dataclass(frozen=True)
class ActivityLifecycle:
    """One execution as its shared lifecycle record describes it.

    Attributes:
        trace_id: The execution's trace (a task run's ``run_id``).
        person_id: The member the execution ran as.
        source: The trace source (``interactive``, ``routine``, ``manual``...).
        command: The command the execution ran.
        status: The lifecycle state, in the record's own vocabulary.
        started_at: When the execution started.
        ended_at: When it ended, or empty while it runs.
        attributes: The trace attributes the record mirrors (first-seen).
        summary: The member's completion summary, when the run recorded one.
        has_evidence: Whether the run recorded an outcome or provider evidence.
    """

    trace_id: str
    person_id: str
    source: str
    command: str
    status: str
    started_at: str
    ended_at: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)
    summary: str = ""
    has_evidence: bool = False


def lifecycle_from_run(record: TaskRunRecord) -> ActivityLifecycle:
    """Describe a workflow run from its task-run record."""
    return ActivityLifecycle(
        trace_id=record.run_id,
        person_id=record.member_id,
        # The trace source, not the execution mode: a member's own completion
        # (``member task complete`` outside a boundary) is user-initiated too,
        # and is work worth showing.
        source=record.source,
        command=record.work_kind,
        status=record.status,
        started_at=record.started_at,
        ended_at=record.finished_at or "",
        attributes=record.attributes,
        summary=record.safe_summary if record.result is not None else "",
        has_evidence=record.result is not None or bool(record.provider_evidence),
    )


def lifecycle_from_session(record: Mapping[str, Any]) -> ActivityLifecycle:
    """Describe an interactive session from its session record."""
    attributes = record.get("attributes")
    return ActivityLifecycle(
        trace_id=str(record.get("trace_id") or ""),
        person_id=str(record.get("person_id") or ""),
        source=INTERACTIVE_SOURCE,
        command=str(record.get("command") or ""),
        status=str(record.get("status") or ""),
        started_at=str(record.get("started_at") or ""),
        ended_at=str(record.get("last_seen_at") or ""),
        attributes=attributes if isinstance(attributes, Mapping) else {},
        has_evidence=True,
    )


def build_activity_history(
    *,
    start: datetime,
    end: datetime,
    members: Iterable[Person],
    lifecycles: Iterable[ActivityLifecycle],
    records: Iterable[dict[str, Any]],
) -> ActivityHistoryResponse:
    """Assemble the response from lifecycle records and fact records.

    Args:
        start: The start of the shown window.
        end: The end of the shown window.
        members: The team; humans are not shown.
        lifecycles: One per execution to show as a session.
        records: The fact records (shared activity events, memory audit
            events, work targets) in diagnostics-record shape.
    """
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
    sessions = _build_sessions(lifecycles, ordered_records, display_member_ids)
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
    lifecycles: Iterable[ActivityLifecycle],
    records: list[dict[str, Any]],
    display_member_ids: set[str],
) -> list[ActivityHistorySession]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        trace_id = str(item.get("trace_id") or "")
        if trace_id:
            grouped.setdefault(trace_id, []).append(item)

    sessions: list[ActivityHistorySession] = []
    for lifecycle in lifecycles:
        if not lifecycle.trace_id or lifecycle.person_id not in display_member_ids:
            continue
        summary = _summarize_trace(lifecycle, grouped.get(lifecycle.trace_id, []))
        if summary is not None:
            sessions.append(summary)
    sessions.sort(key=lambda session: _timestamp_sort_key(session.started_at))
    return sessions


def _summarize_trace(
    lifecycle: ActivityLifecycle, records: list[dict[str, Any]]
) -> ActivityHistorySession | None:
    if lifecycle.source == MANUAL_SESSION_SOURCE:
        return None
    started_at = parse_timestamp(lifecycle.started_at)
    if started_at is None:
        return None
    ended_at = parse_timestamp(lifecycle.ended_at) or _latest_timestamp(
        records, started_at
    )
    # The lifecycle record holds the trace's attributes as recorded at its
    # start, so it precedes every fact record in the first-seen merge. It is
    # one more record for the read-only rule: a session whose first target
    # was only inspected names it, but does not link to it.
    described = [{"attributes": dict(lifecycle.attributes)}, *records]
    attributes = first_seen_attributes(described)
    status = TraceStatus()
    status.observe(lifecycle.status)
    for item in records:
        status.add(item)
    resolved = status.resolve()
    # A read (PR / issue inspect, memory recall) names what the session looked
    # at, which titles it, but is not its work, so it yields no link.
    worked = [item for item in described if not is_read_only_record(item)]
    links = links_from_records(worked, first_seen_attributes(worked))
    rate_limit = _rate_limit_from_records(records)
    mode: ActivitySessionMode = (
        "interactive" if lifecycle.source == INTERACTIVE_SOURCE else "workflow"
    )
    if (
        mode == "workflow"
        and resolved in _QUIET_STATUSES
        and rate_limit is None
        and not lifecycle.has_evidence
        and not _has_workflow_activity_evidence(records, attributes, links)
    ):
        return None
    return ActivityHistorySession(
        trace_id=lifecycle.trace_id,
        person_id=lifecycle.person_id,
        source=lifecycle.source,
        command=lifecycle.command,
        workflow="",
        title=resolve_trace_title(
            records,
            attributes,
            person_id=lifecycle.person_id,
            command=lifecycle.command,
            completion_summary=lambda _attributes, _person_id: lifecycle.summary,
            fallback=lifecycle.trace_id,
        ),
        mode=mode,
        status=resolved,
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        duration_seconds=max(0.0, (ended_at - started_at).total_seconds()),
        links=links,
        rate_limit=rate_limit,
    )


def _latest_timestamp(records: list[dict[str, Any]], floor: datetime) -> datetime:
    """The end of a still-running execution: its latest record, at the earliest."""
    latest = floor
    for item in records:
        parsed = parse_timestamp(str(item.get("timestamp") or ""))
        if parsed is not None and parsed > latest:
            latest = parsed
    return latest


def _has_workflow_activity_evidence(
    records: list[dict[str, Any]],
    attributes: dict[str, Any],
    links: list[ActivityHistoryLink],
) -> bool:
    if links:
        return True
    if _has_work_target_attributes(attributes):
        return True
    return any(str(item.get("kind") or "") == "memory" for item in records)


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
