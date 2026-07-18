"""Provider-neutral conversation, execution, event, and error contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class ResumePolicy(StrEnum):
    FRESH = "fresh"
    RESUME = "resume"
    AUTO = "auto"
    RESET = "reset"


class AgentEventKind(StrEnum):
    PROCESS = "process"
    TURN = "turn"
    ASSISTANT = "assistant"
    COMMAND = "command"
    FILE_CHANGE = "file_change"
    TOOL = "tool"
    APPROVAL = "approval"
    USAGE = "usage"
    FAILED = "failed"


class AgentRuntimeErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    PROTOCOL = "protocol"
    PROCESS = "process"
    CANCELLED = "cancelled"
    SESSION_UNAVAILABLE = "session_unavailable"
    UNSUPPORTED_VERSION = "unsupported_version"


@dataclass(frozen=True, slots=True)
class ConversationKey:
    person_id: str
    adapter: str
    work_kind: str
    work_identity: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.person_id,
                self.adapter,
                self.work_kind,
                self.work_identity,
            )
        ):
            raise ValueError("Conversation key fields must not be empty.")

    @property
    def stable_id(self) -> str:
        import hashlib

        source = "\0".join(
            (self.person_id, self.adapter, self.work_kind, self.work_identity)
        )
        return hashlib.sha256(source.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentExecutionContext:
    person_id: str
    run_id: str
    cwd: Path
    workspace_data_root: Path
    conversation_key: ConversationKey
    resume_policy: ResumePolicy = ResumePolicy.AUTO
    context_cursor: str = ""
    event_id: str = ""
    lease_id: str = ""
    delegation_id: str = ""
    model: str = ""
    rebuild_context: str = ""
    rebuild_context_complete: bool = False
    attempt: int = 1
    continuation_input: str = ""
    participant_labels: str = ""

    def __post_init__(self) -> None:
        if self.person_id != self.conversation_key.person_id:
            raise ValueError("Execution and conversation person_id must match.")
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty.")


@dataclass(slots=True)
class ConversationRecord:
    key: ConversationKey
    generation: int = 0
    provider_session_id: str = ""
    provider_turn_id: str = ""
    context_cursor: str = ""
    last_event_id: str = ""
    last_run_id: str = ""
    provider: str = ""
    model: str = ""
    healthy: bool = True
    turn_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    created_at: str = ""
    updated_at: str = ""
    rotation_reason: str = ""

    def rotate(self, reason: str) -> None:
        self.generation += 1
        self.provider_session_id = ""
        self.provider_turn_id = ""
        self.context_cursor = ""
        self.last_event_id = ""
        self.last_run_id = ""
        self.healthy = True
        self.turn_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.rotation_reason = reason


@dataclass(frozen=True, slots=True)
class AgentEvent:
    kind: AgentEventKind
    name: str
    message: str = ""
    provider_session_id: str = ""
    provider_turn_id: str = ""
    item_id: str = ""
    command: str = ""
    path: str = ""
    approval: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentTerminalResult:
    output: str
    events: tuple[AgentEvent, ...]
    provider_session_id: str
    provider_turn_id: str = ""
    finish_reason: str = "completed"
    usage: dict[str, int] = field(default_factory=dict)
    stderr: str = ""
    returncode: int = 0


class AgentRuntimeError(RuntimeError):
    def __init__(
        self,
        category: AgentRuntimeErrorCategory,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        rotate_session: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.details = dict(details or {})
        self.rotate_session = rotate_session


EventSink = Callable[[AgentEvent], Awaitable[None] | None]


class AgentAdapter(Protocol):
    name: str

    async def run_turn(
        self,
        prompt: str,
        context: AgentExecutionContext,
        conversation: ConversationRecord,
        emit: EventSink,
    ) -> AgentTerminalResult: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...
