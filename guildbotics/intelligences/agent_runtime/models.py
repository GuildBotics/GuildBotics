"""Provider-neutral conversation, execution, event, and error contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from guildbotics.runtime.member_invocation import Work
from guildbotics.runtime.person_lease import PersonExecutionLease


class ResumePolicy(StrEnum):
    FRESH = "fresh"
    RESUME = "resume"
    AUTO = "auto"


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
    #: The turn's settings ask for something the provider cannot enforce on
    #: this device; nothing was started.
    CONFIGURATION = "configuration"


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


@dataclass(slots=True)
class TurnLogin:
    """Whether the login a turn was lent could be used, as its environment
    knows it.

    The tool holds a stand-in, so it meets a refused login only in what the
    gateway answers, and reports it in its own words or not at all; the
    environment knows, and tells the turn here (``refusal`` is why, or
    nothing).
    """

    refusal: Callable[[], str] = lambda: ""


@dataclass(frozen=True, slots=True)
class AgentExecutionContext:
    person_id: str
    run_id: str
    cwd: Path
    conversation_key: ConversationKey
    resume_policy: ResumePolicy = ResumePolicy.AUTO
    context_cursor: str = ""
    event_id: str = ""
    #: The execution lease the turn holds, which the member commands it asks for
    #: act under; a read-only turn holds none and so cannot ask for a write.
    lease: PersonExecutionLease | None = None
    model: str = ""
    #: Resolved provider-neutral effort level (``low`` / ``high``), or ``""``
    #: when the turn requests no effort override.
    effort: str = ""
    #: Provider-specific settings for that level. Each adapter allowlists the
    #: keys it understands and warns about the rest instead of ignoring them.
    provider_options: dict[str, Any] = field(default_factory=dict)
    rebuild_context: str = ""
    rebuild_context_complete: bool = False
    attempt: int = 1
    continuation_input: str = ""
    participant_labels: str = ""
    #: The diagnostics trace the turn runs inside. The member commands the
    #: broker runs record into it, so what the agent read or changed shows up on
    #: the execution that asked for it.
    trace_id: str = ""
    login: TurnLogin = field(default_factory=TurnLogin)
    #: The work the host's grant holds the turn to, which the member commands
    #: it asks for act on; only the host, which settles it, has it.
    work: Work | None = None

    def __post_init__(self) -> None:
        if self.person_id != self.conversation_key.person_id:
            raise ValueError("Execution and conversation person_id must match.")
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty.")

    @property
    def lease_id(self) -> str:
        """The held lease's id for diagnostics, or an empty string without one."""
        return self.lease.metadata.lease_id if self.lease is not None else ""


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
    #: Last non-empty model and effort values a finished turn reported or
    #: imposed. Adapters may reapply them on resume;
    #: when a turn reports no value, the brain retains the last known one.
    effective_model: str = ""
    effective_effort: str = ""
    healthy: bool = True
    turn_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Absolute size of the provider's session context at the last turn, not a
    # per-turn token count: it is replaced, never accumulated.
    context_used_tokens: int = 0
    context_size_tokens: int = 0
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
        self.context_used_tokens = 0
        self.context_size_tokens = 0
        # Settings belong to the session that is being discarded.
        self.effective_model = ""
        self.effective_effort = ""
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


#: The turn event every adapter reports when its provider compacted the
#: session's history, which marks the conversation for rotation.
CONTEXT_COMPACTION = "context_compaction"


def context_compaction_event(
    provider_session_id: str,
    details: dict[str, Any],
    *,
    provider_turn_id: str = "",
    item_id: str = "",
) -> AgentEvent:
    """The event reporting that the provider compacted the session's history."""
    return AgentEvent(
        AgentEventKind.TURN,
        CONTEXT_COMPACTION,
        provider_session_id=provider_session_id,
        provider_turn_id=provider_turn_id,
        item_id=item_id,
        details=details,
    )


def model_and_effort(
    context: AgentExecutionContext, efforts: frozenset[str]
) -> dict[str, Any]:
    """The model and effort a turn's provider options name, as a CLI takes them.

    The model is kept as named; the effort only when it is one of the
    ``efforts`` the CLI accepts. Silent by design: callers warn about what is
    dropped where it is dropped.
    """
    settings: dict[str, Any] = {}
    if model := str(context.provider_options.get("model", "") or "").strip():
        settings["model"] = model
    effort = str(context.provider_options.get("effort", "") or "").strip().lower()
    if effort in efforts:
        settings["effort"] = effort
    return settings


def command_line(command: Any) -> str:
    """A provider's command, given as one string or as argv, as one line."""
    if isinstance(command, str):
        return command
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return ""


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
    #: The model the turn really ran on: the one the provider reported, or the
    #: one the adapter itself imposed. Empty when neither is known, because an
    #: invented effective value is worse than an absent one.
    model: str = ""
    #: The effort the turn really ran under, in the provider's own vocabulary
    #: when it reports one and in the provider-neutral one when the adapter
    #: imposed it. Empty when nothing was applied or reported.
    effort: str = ""


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
