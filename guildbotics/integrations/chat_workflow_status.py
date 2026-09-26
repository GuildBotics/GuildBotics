from __future__ import annotations

from typing import Any

from guildbotics.integrations.chat_service import ChatEvent

WORKFLOW_STATUS_EVENT_TYPE = "guildbotics.workflow_status"
WORKFLOW_STATUS_KIND = "workflow_error"
WORKFLOW_STATUS_ROUTING_SUPPRESS = "suppress"


def workflow_status_fields(
    *,
    reason: str,
    person_id: str,
    run_id: str,
    retry_after_at: str = "",
    retry_after_text: str = "",
    source_event_id: str = "",
    subject_id: str = "",
) -> dict[str, object]:
    """A workflow status, as chat metadata and GitHub comment markers carry it.

    Args:
        reason: Why the run stopped.
        person_id: The member whose run it reports on.
        run_id: The run it reports on.
        retry_after_at: When a rate-limited run can be retried, if known.
        retry_after_text: The provider's own words for that, if any.
        source_event_id: The chat event the run answered, for a chat status.
        subject_id: The issue or pull request URL, for a GitHub status.
    """
    fields: dict[str, object] = {
        "kind": WORKFLOW_STATUS_KIND,
        "routing": WORKFLOW_STATUS_ROUTING_SUPPRESS,
        "reason": reason,
        "person_id": person_id,
        "run_id": run_id,
    }
    optional = {
        "retry_after_at": retry_after_at,
        "retry_after_text": retry_after_text,
        "source_event_id": source_event_id,
        "subject_id": subject_id,
    }
    fields.update({key: value for key, value in optional.items() if value})
    return fields


def workflow_status_metadata(fields: dict[str, object]) -> dict[str, object]:
    """Chat message metadata carrying ``fields`` from :func:`workflow_status_fields`."""
    return {"event_type": WORKFLOW_STATUS_EVENT_TYPE, "event_payload": fields}


def normalize_workflow_status_metadata(metadata: object) -> dict[str, object]:
    """Rebuild workflow-status metadata from its own schema.

    This module owns the shape, so normalization reconstructs exactly the
    fields ``workflow_status_metadata`` writes instead of passing the stored
    dict through — anything else has no slot and disappears.
    """
    if not isinstance(metadata, dict):
        return {}
    if metadata.get("event_type") != WORKFLOW_STATUS_EVENT_TYPE:
        return {}
    payload = metadata.get("event_payload")
    if not isinstance(payload, dict):
        return {}
    rebuilt: dict[str, object] = {
        "kind": str(payload.get("kind") or ""),
        "routing": str(payload.get("routing") or ""),
        "reason": str(payload.get("reason") or ""),
        "person_id": str(payload.get("person_id") or ""),
        "source_event_id": str(payload.get("source_event_id") or ""),
        "run_id": str(payload.get("run_id") or ""),
    }
    for optional in ("retry_after_at", "retry_after_text"):
        value = str(payload.get(optional) or "")
        if value:
            rebuilt[optional] = value
    return {
        "event_type": WORKFLOW_STATUS_EVENT_TYPE,
        "event_payload": rebuilt,
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
