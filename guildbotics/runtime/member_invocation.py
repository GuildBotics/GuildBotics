"""Per-invocation context for member capability execution."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from guildbotics.runtime.person_lease import PersonExecutionLease


@dataclass(frozen=True, slots=True)
class MemberInvocation:
    """Execution metadata shared by one member command invocation.

    ``lease`` is the execution lease of the turn that asked for the command:
    holding it is what lets a workflow's member command write as that person.
    """

    run_id: str = ""
    task_run_id: str = ""
    participant_labels: str = ""
    trace_id: str = ""
    lease: PersonExecutionLease | None = None


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
