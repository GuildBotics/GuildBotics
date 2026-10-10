from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from guildbotics.runtime.member_invocation import ChatSubject, Work

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
    """A workflow run the host started.

    ``run_id`` and ``work`` are what the host selected the run for: the run
    its command records to, and the work its grant holds it to. A ticket or
    chat run has both; any other has neither.
    """

    command: str
    person_id: str
    source: WorkflowSource
    trigger_type: WorkflowTriggerType
    payload: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    work: Work | None = None


class ChatTurn(BaseModel):
    """Everything the chat workflow needs to run one AI CLI turn: the payload
    of a chat workflow's invocation, whose work is the turn's ``subject``."""

    attempt: int
    subject: ChatSubject
    message_ts: str
    context_cursor: str
    effort: str = ""
    prompt: dict[str, Any]
