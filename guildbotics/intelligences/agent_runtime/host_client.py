"""What a command's isolated environment asks of the host, from inside it.

A command runs in its microVM; what only the host holds -- the login a turn
is lent, the run record, the conversation ledger, the diagnostics -- it asks
the host for through the command's window (the member broker's
``/host/<call>`` route, answered under the command's grant), with the URL and
token it was started with. This module is that client, and the wire the host
reads it by; it imports nothing only the host may hold.

The command's facts reach the environment when it boots, as variables: the
window (:data:`HOST_URL_ENV`, :data:`HOST_TOKEN_ENV`), what the command is
(:data:`COMMAND_ENV`, a :class:`CommandFacts`), and the workspace the way the
environment spells it (``GUILDBOTICS_WORKSPACE_ROOT`` and
``GUILDBOTICS_CONFIG_DIR``, read by :mod:`guildbotics.utils.fileio`). They are
fixed for the command, as its grant is, so none is asked for. What to run
reaches it as a :class:`CommandRequest` on the entry's standard input, and
how it ended leaves as a :class:`CommandReply` on its standard output.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

import httpx
from pydantic import BaseModel, Field, TypeAdapter

from guildbotics.commands.metadata import CommandAccess
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)
from guildbotics.observability import SpanContext
from guildbotics.runtime.workflow_invocation import WorkflowInvocation

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
#: How long a call may take: the host gives it as long as a member command,
#: and answers a call it gave up on.
_CALL_SECONDS = 330.0

_RECORD = TypeAdapter(ConversationRecord)


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


class HostClient:
    """The command's window to the host, at ``url`` with ``token``."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    def call(self, name: str, **arguments: Any) -> Any:
        """Ask the host for ``name`` and wait for its answer.

        Raises:
            AgentRuntimeError: When the host failed it with a turn's error.
            HostCallError: When the host refused it, failed it otherwise, or
                was not there to ask.
        """
        try:
            response = httpx.post(
                f"{self._url}/{name}",
                json=arguments,
                headers=self._headers,
                timeout=_CALL_SECONDS,
            )
        except httpx.TransportError as exc:
            raise HostCallError("unavailable", str(exc)) from exc
        return _result(response)

    async def acall(self, name: str, **arguments: Any) -> Any:
        """:meth:`call`, without holding the event loop."""
        try:
            async with httpx.AsyncClient(timeout=_CALL_SECONDS) as client:
                response = await client.post(
                    f"{self._url}/{name}", json=arguments, headers=self._headers
                )
        except httpx.TransportError as exc:
            raise HostCallError("unavailable", str(exc)) from exc
        return _result(response)

    async def begin_turn(
        self, tool: str, cwd: str, *, participant_labels: str = ""
    ) -> HostTurn:
        """Start a turn of ``tool`` working in ``cwd``, doing the command's
        work; the host lends the turn its login."""
        answer = await self.acall(
            "begin_turn",
            tool=tool,
            cwd=cwd,
            participant_labels=participant_labels,
        )
        return HostTurn(**answer)

    async def end_turn(self, turn_grant: str) -> str:
        """End the turn; why the login it was lent could not be used, if so."""
        return str((await self.acall("end_turn", turn_grant=turn_grant))["refusal"])

    async def record(self, entries: Sequence[BaseModel]) -> None:
        """Have the host write ``entries``, in order, in the command's trace."""
        await self.acall(
            "record", entries=[entry.model_dump(mode="json") for entry in entries]
        )


def command_window() -> HostClient | None:
    """The command's window to the host, when this process runs inside the
    command's isolated environment; none on the host."""
    url = os.environ.get(HOST_URL_ENV)
    return HostClient(url, os.environ[HOST_TOKEN_ENV]) if url else None


class ClientRunLedger:
    """The command's run record, through the host (a ``RunLedger``)."""

    def __init__(self, client: HostClient, facts: CommandFacts) -> None:
        self._client = client
        self.run_id = facts.run_id
        self.work_kind = facts.work_kind

    def require_completion(self) -> None:
        """Raise unless the run has recorded a terminal completion."""
        self._client.call("require_completion")

    def evidence(self) -> list[dict[str, Any]]:
        """Return the evidence the run has recorded so far."""
        return list(self._client.call("evidence"))

    def record_completed(self, attempt: int) -> None:
        """Record that the run's completion was found after an attempt."""
        self._client.call("record_completed", attempt=attempt)

    def record_completion_missing(
        self, attempt: int, max_attempts: int, error: str
    ) -> None:
        """Record an attempt that ended without the run's completion."""
        self._client.call(
            "record_completion_missing",
            attempt=attempt,
            max_attempts=max_attempts,
            error=error,
        )


class ClientConversationStore:
    """The conversation ledger, through the host.

    What the host saved comes back into the caller's record, as it does from
    the ledger itself.
    """

    def __init__(self, client: HostClient) -> None:
        self._client = client

    def resolve(
        self,
        key: ConversationKey,
        policy: ResumePolicy,
        *,
        model: str = "",
    ) -> ConversationRecord:
        answer = self._client.call(
            "resolve",
            key=asdict(key),
            policy=policy.value,
            model=model,
        )
        return _RECORD.validate_python(answer)

    def save(self, record: ConversationRecord) -> None:
        _adopt(record, self._client.call("save", record=_dump(record)))

    def mark_unhealthy(self, record: ConversationRecord, reason: str) -> None:
        _adopt(
            record,
            self._client.call("mark_unhealthy", record=_dump(record), reason=reason),
        )


def _dump(record: ConversationRecord) -> dict[str, Any]:
    return _RECORD.dump_python(record, mode="json")


def _adopt(record: ConversationRecord, answer: Any) -> None:
    saved = _RECORD.validate_python(answer)
    for each in fields(ConversationRecord):
        setattr(record, each.name, getattr(saved, each.name))


def _result(response: httpx.Response) -> Any:
    """What the host answered, or the error it answered with."""
    if response.status_code == httpx.codes.OK:
        return response.json()["result"]
    try:
        error = response.json()["error"]
    except (ValueError, KeyError, TypeError):
        raise HostCallError(
            "refused", f"The host refused the call ({response.status_code})."
        ) from None
    category = str(error.get("category") or "failed")
    message = str(error.get("message") or "")
    details = error.get("details") or {}
    if category in AgentRuntimeErrorCategory:
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory(category), message, details=details
        )
    raise HostCallError(category, message, details)
