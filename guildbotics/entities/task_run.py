"""Shared durable record for one workflow or command execution."""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from guildbotics.utils.workspace_sync_port import SHARED_RECORD_SCHEMA_VERSION

TaskRunExecutionMode = Literal["autonomous", "user_initiated", "remote"]
TaskRunState = Literal[
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
    "result_unknown",
]
TASK_RUN_TERMINAL_STATES = frozenset(
    state for state in get_args(TaskRunState) if state != "running"
)
TASK_RUN_UNDONE_STATES = frozenset({"failed", "cancelled", "interrupted"})
"""Terminal states that tell us the run's work did not take effect.

``result_unknown`` is deliberately not one of them: it means the process that
was doing the work disappeared before its outcome could be observed, so the
work may already have happened outside this process. Such a run stays terminal
and is repeated only when the user starts it again.
"""


class TaskRunResult(BaseModel):
    """Safe subject metadata recorded when a workflow reaches a result."""

    model_config = ConfigDict(extra="forbid")

    subject_type: str
    subject_id: str
    subject_url: str = ""
    status: Literal["done", "asking", "blocked"]


class TaskRunRecord(BaseModel):
    """The single JSON object stored at ``state/task-runs/<run_id>/result.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=SHARED_RECORD_SCHEMA_VERSION)
    run_id: str
    work_kind: str
    work_identity: dict[str, str] | None = None
    execution_mode: TaskRunExecutionMode
    member_id: str
    device_id: str
    #: The source of the trace the run is (``routine``, ``event_listener``, ...),
    #: so another device can place the run on its timeline without the trace.
    source: str = ""
    #: The trace's attributes as the run's boundary saw them -- the PR / issue
    #: or chat thread the run targets. Mirrored so the run names its work on
    #: every device, not only the one holding the trace.
    attributes: dict[str, str] = Field(default_factory=dict)
    started_at: str
    finished_at: str | None = None
    status: TaskRunState = "running"
    safe_summary: str = ""
    result: TaskRunResult | None = None
    provider_evidence: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def left_work_undone(self) -> bool:
        """Whether the run ended in a state that proves its work did not happen.

        A run that raised, was cancelled, or lost its owner device left the
        work undone, so the same work identity may be attempted again under
        this run. A run that recorded a result is done, including the
        ``asking`` and ``blocked`` completions stored with a ``failed``
        status, and so is a succeeded run that carries no subject result. A
        ``result_unknown`` run is not known to have left its work undone, so
        it is never picked up again on its own.
        """
        return self.status in TASK_RUN_UNDONE_STATES and self.result is None
