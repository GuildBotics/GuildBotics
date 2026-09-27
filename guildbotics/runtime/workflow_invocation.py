from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

WorkflowSource = Literal[
    "routine",
    "scheduled",
    "event_queue",
    "manual",
]

WorkflowTriggerType = Literal[
    "ticket",
    "chat",
    "scheduled",
    "generic",
]

WORKFLOW_INVOCATION_KEY = "workflow_invocation"

#: The workflow every route runs through the host's ticket selector.
TICKET_WORKFLOW_COMMAND = "workflows/ticket_driven_workflow"


@dataclass(frozen=True, slots=True)
class WorkflowInvocation:
    command: str
    person_id: str
    source: WorkflowSource
    trigger_type: WorkflowTriggerType
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""


class ChatTurn(BaseModel):
    """Everything the chat workflow needs to run one AI CLI turn: the payload
    of a chat workflow's invocation."""

    run_id: str
    attempt: int
    service_name: str
    channel_id: str
    thread_ts: str
    event_id: str
    message_ts: str
    work_identity: str
    context_cursor: str
    effort: str = ""
    prompt: dict[str, Any]
