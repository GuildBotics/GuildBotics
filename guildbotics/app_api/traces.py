"""Read what this device recorded: traces, memory events, AI CLI turns and
inference calls."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from guildbotics.app_api.activity_history import run_subject_id
from guildbotics.app_api.agent_streams import collapse_assistant_streams
from guildbotics.app_api.models import (
    CliAgentLastTurn,
    InferenceFailuresResponse,
    InferenceFailureStatus,
    MemoryEvent,
    MemoryEventsResponse,
    TraceDetailResponse,
    TraceRecord,
    TracesResponse,
    TraceSummary,
)
from guildbotics.capabilities.member_memory_audit import (
    MemoryAuditStore,
    parse_memory_audit_timestamp,
)
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.drivers.trace_presentations import normalize_trace_presentation
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.observability.trace_title import CompletionSummary
from guildbotics.utils.fileio import get_workspace_state_path
from guildbotics.utils.timestamps import parse_iso_datetime

MIN_MEMORY_DOCUMENT_PATH_PARTS = 2
#: The services whose calls use a workspace key.
KEY_SERVICES = frozenset({"llm", "jev"})


def list_traces(
    store: DiagnosticsStore,
    *,
    source: str | None = None,
    person_id: str | None = None,
    query: str | None = None,
    attr_key: str | None = None,
    attr_value: str | None = None,
    limit: int = 200,
) -> TracesResponse:
    """The traces recorded on this device that match the filters."""
    summaries = store.list_traces(
        source=source,
        person_id=person_id,
        query=query,
        attr_key=attr_key,
        attr_value=attr_value,
        limit=limit,
        completion_summary=_completion_summary_lookup(),
    )
    return TracesResponse(
        traces=[TraceSummary.model_validate(summary) for summary in summaries]
    )


def trace_detail(store: DiagnosticsStore, trace_id: str) -> TraceDetailResponse:
    """One trace's records, with the memory events recorded in it."""
    raw_summary = store.get_summary(trace_id, _completion_summary_lookup())
    records = [
        _to_trace_record(item)
        for item in collapse_assistant_streams(store.get_records(trace_id))
    ]
    records.extend(
        _to_trace_record(item)
        for item in MemoryAuditStore().list_events(trace_id=trace_id, limit=1000)
    )
    records.sort(key=_trace_record_sort_key)
    return TraceDetailResponse(
        trace_id=trace_id,
        summary=(
            TraceSummary.model_validate(raw_summary)
            if raw_summary is not None
            else None
        ),
        records=records,
        transcript_available=store.transcript_exists(trace_id),
    )


def global_records(store: DiagnosticsStore, limit: int) -> TraceDetailResponse:
    """The latest records outside any trace, under the current system session."""
    trace_id = store.latest_system_trace_id() or ""
    raw_summary = store.get_summary(trace_id) if trace_id else None
    records = [_to_trace_record(item) for item in store.global_records(limit=limit)]
    return TraceDetailResponse(
        trace_id=trace_id,
        summary=(
            TraceSummary.model_validate(raw_summary)
            if raw_summary is not None
            else None
        ),
        records=records,
        transcript_available=bool(records),
    )


def list_memory_events(**filters: Any) -> MemoryEventsResponse:
    """The memory events matching ``MemoryAuditStore.list_events`` filters."""
    events = MemoryAuditStore().list_events(**filters)
    return MemoryEventsResponse(
        event_count=len(events), events=[_memory_event(item) for item in events]
    )


def last_cli_agent_turns(store: DiagnosticsStore) -> list[CliAgentLastTurn]:
    """The last CLI turn per member and tool on this device.

    Aggregates the retained index, including rows outside its in-memory tail.
    Tool identity and model selection belong to the recorded turn. Current
    slot settings cannot reconstruct either fact after configuration changes.
    """
    records, _ = store.records_after(
        None,
        includes=lambda item: (
            item.get("type") in {"span.finished", "span.failed"}
            and item.get("attributes", {}).get("agent.kind") == "cli_agent"
        ),
    )
    latest: dict[tuple[str, str], tuple[datetime, CliAgentLastTurn]] = {}
    for record in records:
        person_id = record.get("person_id", "")
        agent = record.get("attributes", {}).get("agent.adapter", "")
        payload = record.get("payload", {})
        specified = payload.get("model_specified")
        timestamp = record.get("timestamp", "")
        when = parse_iso_datetime(timestamp)
        if not person_id or not agent or type(specified) is not bool or when is None:
            continue
        key = (person_id, agent)
        previous = latest.get(key)
        if previous is None or when >= previous[0]:
            latest[key] = (
                when,
                CliAgentLastTurn(
                    person_id=person_id,
                    agent=agent,
                    model=payload.get("model", ""),
                    model_specified=specified,
                    effort=payload.get("effort", ""),
                    timestamp=timestamp,
                ),
            )
    return [latest[key][1] for key in sorted(latest)]


def inference_key(record: dict[str, Any]) -> tuple[str, str] | None:
    """The ``(service, provider)`` whose key an inference call's span used,
    or None for any other record."""
    if record.get("kind") != "event" or record.get("type") not in {
        "span.failed",
        "span.finished",
    }:
        return None
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        return None
    service = attributes.get("credential.provider")
    if service not in KEY_SERVICES:
        return None
    return str(service), str(attributes.get("llm.provider") or "")


def latest_inference_failures(store: DiagnosticsStore) -> InferenceFailuresResponse:
    """The refusal each key's latest call ended with, if it was refused.

    Every inference call made with a workspace key (an LLM provider's, Jev's)
    ends its span naming the key's service and provider and, when the provider
    refused it, why. The latest such call per key is its state: a refusal shows
    until a call of the same key succeeds. A call that failed without the
    provider saying why (cancelled) tells nothing about the key, so it does
    not replace the previous outcome.
    """
    records, _ = store.records_after(
        None, includes=lambda item: inference_key(item) is not None
    )
    latest: dict[tuple[str, str], tuple[datetime, InferenceFailureStatus | None]] = {}
    for record in records:
        key = inference_key(record)
        timestamp = str(record.get("timestamp") or "")
        when = parse_iso_datetime(timestamp)
        attributes = record["attributes"]
        category = attributes.get("error.category")
        failed = record.get("type") == "span.failed"
        if key is None or when is None or (failed and not category):
            continue
        previous = latest.get(key)
        if previous is None or when >= previous[0]:
            latest[key] = (
                when,
                InferenceFailureStatus(
                    category=category,
                    timestamp=timestamp,
                    status_code=attributes.get("error.status_code"),
                    response=str(attributes.get("error.response") or ""),
                )
                if failed
                else None,
            )
    llm = {
        provider: failure
        for (service, provider), (_, failure) in latest.items()
        if service == "llm" and provider and failure
    }
    jev = latest.get(("jev", ""), (None, None))[1]
    return InferenceFailuresResponse(llm=llm, jev=jev)


def _completion_summary_lookup() -> CompletionSummary:
    """Resolve a trace's recorded run completion summary from its attributes.

    The run records are read once per request, and only if some trace asks
    (``resolve_trace_title`` asks only for a trace that names no PR / issue),
    so listing traces stays cheap.
    """
    store = RunStore()
    summaries: dict[tuple[str, str], str] | None = None

    def lookup(attributes: Mapping[str, Any], person_id: str) -> str:
        nonlocal summaries
        if summaries is None:
            summaries = store.summaries_by_subject()
        return summaries.get((run_subject_id(attributes), person_id), "")

    return lookup


def _to_trace_record(item: dict[str, Any]) -> TraceRecord:
    attributes = item.get("attributes")
    payload = item.get("payload")
    return TraceRecord(
        kind=str(item.get("kind", "")),
        timestamp=str(item.get("timestamp", "")),
        trace_id=item.get("trace_id"),
        span_id=item.get("span_id"),
        parent_id=item.get("parent_id"),
        call_id=item.get("call_id"),
        span=str(item.get("span") or ""),
        source=str(item.get("source") or ""),
        person_id=str(item.get("person_id") or ""),
        command=str(item.get("command") or ""),
        workflow=str(item.get("workflow") or ""),
        type=str(item.get("type") or ""),
        level=str(item.get("level") or ""),
        message=str(item.get("message") or ""),
        attributes=attributes if isinstance(attributes, dict) else {},
        payload=payload if isinstance(payload, dict) else {},
        presentation=normalize_trace_presentation(item),
    )


def _trace_record_sort_key(record: TraceRecord) -> datetime:
    return parse_memory_audit_timestamp(record.timestamp) or datetime.min.replace(
        tzinfo=UTC
    )


def _memory_event(item: dict[str, Any]) -> MemoryEvent:
    raw_attributes = item.get("attributes")
    raw_payload = item.get("payload")
    attributes = (
        cast(dict[str, Any], raw_attributes) if isinstance(raw_attributes, dict) else {}
    )
    payload = cast(dict[str, Any], raw_payload) if isinstance(raw_payload, dict) else {}
    source = payload.get("source")
    changed_fields = payload.get("changed_fields")
    query_keywords = payload.get("query_keywords")
    path = str(attributes.get("memory.path") or "")
    action = str(
        attributes.get("memory.action")
        or str(item.get("type") or "").removeprefix("memory.")
    )
    return MemoryEvent(
        timestamp=str(item.get("timestamp") or ""),
        action=action,
        person_id=str(item.get("person_id") or ""),
        scope=str(attributes.get("memory.scope") or ""),
        doc_id=str(attributes.get("memory.doc_id") or ""),
        path=path,
        title=str(payload.get("title") or ""),
        summary=str(payload.get("summary") or ""),
        kind=str(attributes.get("memory.kind") or ""),
        trace_id=item.get("trace_id"),
        run_id=str(attributes.get("run_id") or ""),
        task_run_id=str(attributes.get("task_run_id") or ""),
        source=[entry for entry in source if isinstance(entry, dict)]
        if isinstance(source, list)
        else [],
        changed_fields=[
            str(field) for field in changed_fields if isinstance(field, str)
        ]
        if isinstance(changed_fields, list)
        else [],
        query_keywords=[
            str(keyword) for keyword in query_keywords if isinstance(keyword, str)
        ]
        if isinstance(query_keywords, list)
        else [],
        result_count=_optional_int(payload.get("result_count")),
        duration_ms=_optional_float(payload.get("duration_ms")),
        body_preview=_memory_body_preview(path),
    )


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _memory_body_preview(path: str, *, limit: int = 800) -> str:
    parts = path.split("/")
    if len(parts) < MIN_MEMORY_DOCUMENT_PATH_PARTS or parts[0] != "documents":
        return ""
    relative_parts = parts[1:]
    if any(part in {"", ".", ".."} for part in relative_parts):
        return ""
    body_path = get_workspace_state_path("documents", *relative_parts) / "body.md"
    if not body_path.is_file():
        return ""
    try:
        body = body_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return body[:limit]
