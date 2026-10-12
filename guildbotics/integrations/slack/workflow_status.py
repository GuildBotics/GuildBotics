"""The workflow status a Slack message carries, as its message metadata."""

from __future__ import annotations

from typing import Any

from guildbotics.runtime.port import WorkflowStatus

_EVENT_TYPE = "guildbotics.workflow_status"


def status_metadata(status: WorkflowStatus) -> dict[str, Any]:
    """The message metadata that carries ``status``."""
    return {"event_type": _EVENT_TYPE, "event_payload": status.payload()}


def metadata_status(metadata: object) -> WorkflowStatus | None:
    """The status a message's metadata carries; ``None`` when it carries none."""
    if not isinstance(metadata, dict) or metadata.get("event_type") != _EVENT_TYPE:
        return None
    return WorkflowStatus.from_payload(metadata.get("event_payload"))
