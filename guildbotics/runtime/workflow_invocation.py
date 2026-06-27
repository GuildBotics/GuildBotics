from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

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


@dataclass(frozen=True, slots=True)
class WorkflowInvocation:
    command: str
    person_id: str
    source: WorkflowSource
    trigger_type: WorkflowTriggerType
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
