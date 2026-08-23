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
    started_at: str
    finished_at: str | None = None
    status: TaskRunState = "running"
    safe_summary: str = ""
    result: TaskRunResult | None = None
    provider_evidence: list[dict[str, Any]] = Field(default_factory=list)
