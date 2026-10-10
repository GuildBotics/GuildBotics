"""The wire between the host and a command's isolated environment.

What crosses the command's window -- the facts the environment boots with
(:class:`CommandFacts` in :data:`COMMAND_ENV`), what to run and how it ended
(:class:`CommandRequest` / :class:`CommandReply`), the records a turn sends
(:data:`Entry`), the turn the host starts (:class:`HostTurn`), and the
working directory's copy (:class:`CopiedFile` / :class:`ChangedFile`) -- is
read by both sides, so it is shared: the host builds and reads it, and the
client inside the environment (:mod:`guildbotics.guest.host_client`) speaks
it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from pydantic import BaseModel, Field, TypeAdapter

from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    ConversationKey,
)
from guildbotics.runtime.workflow_invocation import WorkflowInvocation
from guildbotics.utils.correlation import SpanContext

#: The command's window to the host, and the token every call carries.
HOST_URL_ENV = "GUILDBOTICS_HOST_URL"
HOST_TOKEN_ENV = "GUILDBOTICS_HOST_TOKEN"
#: What the command is: a :class:`CommandFacts`, as JSON.
COMMAND_ENV = "GUILDBOTICS_COMMAND"
#: The detail of a turn's ``started`` event that says where the turn works;
#: the host records what the environment confines it to there instead.
TURN_WORKING_DIRECTORY = "working_directory"
#: The variable a turn's provider reads the member broker's token from.
MEMBER_BROKER_TOKEN_ENV = "GUILDBOTICS_MEMBER_BROKER_TOKEN"


class HostCallError(Exception):
    """A call the host refused (``category`` ``refused``), could not be
    reached for (``unavailable``: the command's grant ended with it), or that
    failed there; the message is fit to show."""

    def __init__(
        self, category: str, message: str, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.category = category
        self.details = dict(details or {})

    def payload(self) -> dict[str, Any]:
        """The error as the wire carries it."""
        return {
            "category": self.category,
            "message": str(self),
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class CommandFacts:
    """What the running command is, as its environment is told.

    ``run_id`` is the run the command's grant covers, and ``work_kind`` and
    ``work_identity`` the work it does, which key the member's conversation
    with each tool its turns run. ``access`` is what the command declared, and ``inspected``
    the directories its turns inspect, as the environment spells them.
    ``mounts`` is what the microVM mounted, by where, and whether a command or
    a turn may work under it (see :func:`admits`).
    """

    person_id: str
    run_id: str
    work_kind: str
    work_identity: str
    trace_id: str
    access: CommandAccess
    inspected: dict[str, str] = field(default_factory=dict)
    mounts: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def read(cls, environ: Mapping[str, str]) -> CommandFacts:
        """The facts the environment was started with."""
        return TypeAdapter(cls).validate_json(environ[COMMAND_ENV])

    def dump(self) -> str:
        """The facts as :data:`COMMAND_ENV` carries them."""
        return TypeAdapter(CommandFacts).dump_json(self).decode()


def admits(mounts: Mapping[str, bool], cwd: str) -> bool:
    """Whether work may happen in ``cwd``: the deepest of ``mounts`` it is
    under is one where it may.

    Args:
        mounts: The microVM's mounts, by where it spells them, and whether
            work may happen under each: what the command's contract opened
            may; what GuildBotics bound for itself and the covers over what
            the contract denies may not.
        cwd: A directory as the microVM spells it.
    """
    path = PurePosixPath(cwd)
    deepest = max(
        (guest for guest in mounts if path.is_relative_to(guest)),
        key=lambda guest: len(PurePosixPath(guest).parts),
        default=None,
    )
    return deepest is not None and mounts[deepest]


class CommandRequest(BaseModel):
    """What the command's environment runs: the main command the host
    resolved (``path``, as the environment spells it), its arguments, where it
    works, its input, and the workflow run it is, if any.

    ``wants_result`` asks for the main command's own result besides its text
    output: only a caller that reads one asks, so a result nobody reads (a
    PDF's bytes) never crosses.
    """

    path: str
    name: str
    args: list[str]
    cwd: str
    pipe: str = ""
    invocation: WorkflowInvocation | None = None
    wants_result: bool = False


class CommandFailure(BaseModel):
    """How the command failed: ``command`` for a command's own failure (a
    ``CommandError``), and the AI CLI tool's failure the error came from, if
    any, as the tool reported it (``cli_agent``, ``message`` and ``result``).
    """

    command: bool
    type: str
    message: str
    cli_agent: str = ""
    cli_agent_message: str = ""
    cli_agent_result: dict[str, Any] | None = None


class CommandReply(BaseModel):
    """How the command ended: its result and text output, or its failure."""

    result: Any = None
    text_output: str = ""
    failure: CommandFailure | None = None


class EventEntry(BaseModel):
    """A turn's event, of the conversation it belongs to."""

    type: Literal["event"] = "event"
    span: SpanContext | None
    conversation: ConversationKey
    generation: int
    context_cursor: str = ""
    event: AgentEvent


class IoEntry(BaseModel):
    """A turn's request or response, the stderr of a response whole."""

    type: Literal["io"] = "io"
    span: SpanContext | None
    io_type: Literal["cli_agent.request", "cli_agent.response"]
    payload: dict[str, Any]


class SummaryEntry(BaseModel):
    """How a turn's span ended, and what it ran on."""

    type: Literal["summary"] = "summary"
    span: SpanContext | None
    slot: str
    tool: str
    model_specified: bool
    status: Literal["finished", "failed"]
    model: str = ""
    effort: str = ""
    duration_ms: float | None = None
    usage: dict[str, int] | None = None


class CredentialEntry(BaseModel):
    """What a turn proved of its tool's login on this device."""

    type: Literal["credential"] = "credential"
    span: SpanContext | None
    tool: str
    failed: bool


#: One record the command's environment sends the host to write.
Entry = Annotated[
    EventEntry | IoEntry | SummaryEntry | CredentialEntry,
    Field(discriminator="type"),
]


@dataclass(frozen=True, slots=True)
class HostTurn:
    """A turn the host started: the grant its member commands carry, the
    environment, working directory and home its provider starts with, the
    microVM's mounts (by where, and whether read-only), and the member
    broker's MCP server (``name``, ``url`` and ``authorization``)."""

    turn_grant: str
    env: dict[str, str]
    cwd: str
    home: str
    mounts: dict[str, bool]
    member: dict[str, str]


#: The workspace's own state a command may let its AI CLI turns inspect:
#: ``diagnostics`` is the recorded runs, ``config`` the workspace
#: configuration and the packaged templates it falls back to.
InspectionScope = Literal["diagnostics", "config"]


@dataclass(frozen=True)
class CommandAccess:
    """What a command declares about the access of its AI CLI turns.

    Mirrors the ``read_only`` / ``inspects`` metadata. A command declares it
    and every turn of its run is held to it: a read-only command's turns can
    change nothing, which is what lets the run take no execution lease and no
    manual-command reservation. ``inspects`` is independent of that: what a
    command needs to read is its work's business.
    """

    read_only: bool = False
    inspects: frozenset[InspectionScope] = frozenset()


#: The most the copied files' list may take, and the changes.
MAX_WORKTREE_LIST_BYTES = 64 * 1024 * 1024
MAX_WORKTREE_CHANGE_BYTES = 1024 * 1024 * 1024


class CopiedFile(TypedDict):
    """A regular file copied: its path relative to the copy, and its state."""

    path: str
    sha256: str
    executable: bool


class ChangedFile(TypedDict):
    """A regular file the command changed: its new content (base64), and
    whether it is executable when the command made it or changed that; or
    that it was deleted. A file whose bit the command left keeps the host's
    mode, whatever happened to it there meanwhile."""

    path: str
    executable: NotRequired[bool]
    content: NotRequired[str]
    deleted: NotRequired[bool]


#: How GuildBotics introduces itself in every peer's ``initialize`` request.
#: Some peers require ``version``: Grok rejects the request without it.
CLIENT_INFO = {"name": "guildbotics", "title": "GuildBotics", "version": "1"}
