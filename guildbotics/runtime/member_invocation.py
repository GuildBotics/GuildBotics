"""Per-invocation context for member capability execution."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from guildbotics.runtime.person_lease import PersonExecutionLease


class GuestProcessError(RuntimeError):
    """A process could not be run in the command's environment, or ran out of
    the invocation's time; the reason is fit to show the agent."""


@dataclass(frozen=True, slots=True)
class GuestResult:
    """How a process in the command's environment ended, and what it wrote.

    ``stdout`` is empty when the caller streamed it to a file of the host.
    """

    returncode: int
    stdout: bytes
    stderr: bytes


class CommandGuest(Protocol):
    """GuildBotics' own processes in the running command's environment.

    What the command's turns can write -- the member's clones among it -- is
    only ever run there, never on the host: a repository the turn wrote
    decides what its git executes (hooks, filters, where it sends the
    member's credential), and inside the environment it decides nothing the
    turn could not do itself.
    """

    def path(self, host: Path) -> str:
        """Where the host ``host`` path is inside the environment."""
        ...

    def python(self, module: str, *args: str) -> list[str]:
        """The command line that runs GuildBotics' own ``module`` with
        ``args`` in the environment, with its own Python and code."""
        ...

    def remaining(self) -> float:
        """Seconds the invocation has left.

        Raises:
            GuestProcessError: When none are left.
        """
        ...

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin: bytes | Path = b"",
        stdout: Path | None = None,
        stdout_limit: int,
    ) -> GuestResult:
        """Run ``argv`` in the environment and wait for it.

        Args:
            argv: The program, found on the guest's PATH, and its arguments.
            cwd: The guest directory it runs in.
            env: What it starts with beyond the environment's facts of the
                host; nothing of the turn's own.
            stdin: What it reads: bytes, or a host file streamed to it.
            stdout: A host file its output is streamed into instead of being
                returned.
            stdout_limit: The most output accepted; more ends the process.

        Raises:
            GuestProcessError: When it cannot run, writes more than
                ``stdout_limit``, or outlasts the invocation.
        """
        ...


@dataclass(frozen=True, slots=True)
class ChatSubject:
    """The chat event a chat workflow run answers, in its source thread."""

    service: str
    channel_id: str
    thread_ts: str
    event_id: str
    #: The member's own user on the service, whose messages are no input.
    self_user_id: str

    @property
    def subject_id(self) -> str:
        """The event as a run's completion names its subject."""
        return f"{self.service}:{self.channel_id}:{self.thread_ts}:{self.event_id}"


@dataclass(frozen=True, slots=True)
class Work:
    """The work a command run does, settled by the host that starts the run.

    ``kind`` and ``identity`` key the member's conversation with each AI CLI
    tool; a ticket is identified by its URL, which is also its subject.
    ``chat`` is the subject of a chat workflow run. Nothing the run passes
    changes any of it.
    """

    kind: str
    identity: str
    chat: ChatSubject | None = None

    @classmethod
    def of_ticket(cls, url: str) -> Work:
        """The work of a ticket workflow run on the ticket at ``url``."""
        return cls("ticket", url)

    @classmethod
    def of_chat(cls, subject: ChatSubject) -> Work:
        """The work of a chat workflow run on ``subject``: the conversation of
        its whole thread."""
        return cls(
            "chat",
            ":".join(
                (
                    subject.service,
                    subject.self_user_id,
                    subject.channel_id,
                    subject.thread_ts,
                )
            ),
            subject,
        )


@dataclass(frozen=True, slots=True)
class MemberInvocation:
    """Execution metadata shared by one member command invocation.

    ``work`` is the work of the run that asked for the command, as its host
    settled it: the subject the command completes or checks is its, never
    one the command is told. ``lease`` is the execution lease of the turn that
    asked for the command: holding it is what lets a workflow's member command
    write as that person. ``guest`` is the environment of the command that
    turn belongs to; outside a command there is none.
    """

    run_id: str = ""
    task_run_id: str = ""
    work: Work | None = None
    participant_labels: str = ""
    trace_id: str = ""
    lease: PersonExecutionLease | None = None
    guest: CommandGuest | None = None


_current_invocation: ContextVar[MemberInvocation | None] = ContextVar(
    "guildbotics_member_invocation", default=None
)
_empty_invocation = MemberInvocation()


def current_member_invocation() -> MemberInvocation:
    """Return the current invocation or an empty context outside member work."""
    return _current_invocation.get() or _empty_invocation


@contextmanager
def member_invocation_scope(invocation: MemberInvocation) -> Iterator[None]:
    """Bind invocation metadata to the current asynchronous context."""
    token = _current_invocation.set(invocation)
    try:
        yield
    finally:
        _current_invocation.reset(token)
