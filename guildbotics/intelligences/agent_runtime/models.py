"""Provider-neutral conversation, execution, event, and error contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from guildbotics.intelligences.sandbox import SandboxContract


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


@dataclass(frozen=True, slots=True)
class AgentExecutionContext:
    person_id: str
    run_id: str
    cwd: Path
    workspace_root: Path
    workspace_data_root: Path
    conversation_key: ConversationKey
    resume_policy: ResumePolicy = ResumePolicy.AUTO
    context_cursor: str = ""
    event_id: str = ""
    lease_id: str = ""
    delegation_id: str = ""
    model: str = ""
    #: Resolved provider-neutral effort level (``low`` / ``high``), or ``""``
    #: when the turn must not intervene in the session's current settings.
    effort: str = ""
    #: Provider-specific settings for that level. Each adapter allowlists the
    #: keys it understands and warns about the rest instead of ignoring them.
    provider_options: dict[str, Any] = field(default_factory=dict)
    rebuild_context: str = ""
    rebuild_context_complete: bool = False
    attempt: int = 1
    continuation_input: str = ""
    participant_labels: str = ""
    # A read-only turn only inspects recorded state, so it holds no execution
    # lease and may run while the member is busy. It is not a weaker sandbox:
    # every turn reads untrusted material, and every turn is confined by the
    # same ``sandbox`` contract.
    read_only: bool = False
    #: What the provider must enforce around ``cwd`` for this turn.
    sandbox: SandboxContract = field(default_factory=SandboxContract)

    def __post_init__(self) -> None:
        if self.person_id != self.conversation_key.person_id:
            raise ValueError("Execution and conversation person_id must match.")
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty.")


def settings_fingerprint(applied: Mapping[str, Any]) -> str:
    """Stable hash of the settings an adapter will actually impose on a session.

    This is deliberately computed from the adapter's own normalized view rather
    than from the requested effort: a request the adapter cannot act on changes
    nothing about the session, so it must not read as a change. An empty mapping
    gives ``""``, which means "keep whatever the session already has" rather
    than "restore the provider default", and a session-scoped adapter therefore
    only rotates when two *non-empty* fingerprints differ.
    """
    import hashlib
    import json

    if not applied:
        return ""
    payload = json.dumps(dict(applied), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


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
    #: Fingerprint of the settings last imposed on this session. Only sessions
    #: whose adapter applies settings per session record one.
    settings_fingerprint: str = ""
    #: What the session is known to really run with: the last non-empty
    #: effective values a finished turn reported. A continued session keeps its
    #: settings when a turn imposes none, so these let such a turn report what
    #: the session still runs under instead of claiming nothing.
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
        # Settings belong to the session that is being discarded; the next turn
        # re-imposes its own, or leaves the fresh session on provider defaults.
        self.settings_fingerprint = ""
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


#: An adapter that can re-send model settings on every turn. Changing effort
#: mid-conversation costs nothing, so the session is never rotated for it.
SETTINGS_SCOPE_TURN = "turn"
#: An adapter whose settings are fixed when the session starts. Changing them
#: requires a fresh session.
SETTINGS_SCOPE_SESSION = "session"


class AgentAdapter(Protocol):
    name: str
    #: How far a settings change reaches; see the constants above.
    settings_scope: str

    def applied_settings(self, context: AgentExecutionContext) -> dict[str, Any]:
        """The settings this adapter will actually impose for the turn.

        Normalized and filtered to what the adapter can really act on, so a
        requested setting it must ignore never looks like a session change.
        """
        ...

    async def run_turn(
        self,
        prompt: str,
        context: AgentExecutionContext,
        conversation: ConversationRecord,
        emit: EventSink,
    ) -> AgentTerminalResult: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...
