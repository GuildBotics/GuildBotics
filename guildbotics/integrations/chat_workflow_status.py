from __future__ import annotations

from typing import Any

from guildbotics.integrations.chat_service import ChatEvent

WORKFLOW_STATUS_EVENT_TYPE = "guildbotics.workflow_status"
WORKFLOW_STATUS_KIND = "workflow_error"
WORKFLOW_STATUS_ROUTING_SUPPRESS = "suppress"


def workflow_status_metadata(
    *,
    reason: str,
    person_id: str,
    source_event_id: str,
    run_id: str,
    retry_after_at: str = "",
    retry_after_text: str = "",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": WORKFLOW_STATUS_KIND,
        "routing": WORKFLOW_STATUS_ROUTING_SUPPRESS,
        "reason": reason,
        "person_id": person_id,
        "source_event_id": source_event_id,
        "run_id": run_id,
    }
    if retry_after_at:
        payload["retry_after_at"] = retry_after_at
    if retry_after_text:
        payload["retry_after_text"] = retry_after_text
    return {
        "event_type": WORKFLOW_STATUS_EVENT_TYPE,
        "event_payload": payload,
    }


def normalize_workflow_status_metadata(metadata: object) -> dict[str, object]:
    if not isinstance(metadata, dict):
        return {}
    if metadata.get("event_type") != WORKFLOW_STATUS_EVENT_TYPE:
        return {}
    payload = metadata.get("event_payload")
    if not isinstance(payload, dict):
        return {}
    return {
        "event_type": WORKFLOW_STATUS_EVENT_TYPE,
        "event_payload": dict(payload),
    }


def is_workflow_status_metadata(metadata: object) -> bool:
    return bool(normalize_workflow_status_metadata(metadata))


def is_suppressed_workflow_status_metadata(metadata: object) -> bool:
    normalized = normalize_workflow_status_metadata(metadata)
    payload = normalized.get("event_payload")
    return (
        isinstance(payload, dict)
        and payload.get("routing") == WORKFLOW_STATUS_ROUTING_SUPPRESS
    )


def is_suppressed_chat_event(event: ChatEvent) -> bool:
    return is_suppressed_workflow_status_metadata(event.metadata)


def workflow_status_payload(metadata: object) -> dict[str, Any]:
    normalized = normalize_workflow_status_metadata(metadata)
    payload = normalized.get("event_payload")
    return dict(payload) if isinstance(payload, dict) else {}
