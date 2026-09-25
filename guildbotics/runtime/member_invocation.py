"""Per-invocation context for member capability execution."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

RUN_ENV = "GUILDBOTICS_RUN_ID"
TASK_RUN_ENV = "GUILDBOTICS_TASK_RUN_ID"
CHAT_PARTICIPANT_LABELS_ENV = "GUILDBOTICS_CHAT_PARTICIPANT_LABELS"
TRACE_ID_ENV = "GUILDBOTICS_TRACE_ID"
LEASE_ID_ENV = "GUILDBOTICS_EXECUTION_LEASE_ID"
DELEGATION_ID_ENV = "GUILDBOTICS_EXECUTION_DELEGATION_ID"
LEASE_PERSON_ENV = "GUILDBOTICS_EXECUTION_PERSON_ID"
LEASE_RUN_ENV = "GUILDBOTICS_EXECUTION_RUN_ID"


@dataclass(frozen=True, slots=True)
class MemberInvocation:
    """Execution metadata shared by one member command invocation."""

    run_id: str = ""
    task_run_id: str = ""
    participant_labels: str = ""
    trace_id: str = ""
    lease_id: str = ""
    delegation_id: str = ""
    lease_person_id: str = ""
    lease_run_id: str = ""

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> MemberInvocation:
        """Read the process boundary once when the member CLI starts."""
        values = os.environ if environ is None else environ
        return cls(
            run_id=values.get(RUN_ENV, ""),
            task_run_id=values.get(TASK_RUN_ENV, ""),
            participant_labels=values.get(CHAT_PARTICIPANT_LABELS_ENV, ""),
            trace_id=values.get(TRACE_ID_ENV, ""),
            lease_id=values.get(LEASE_ID_ENV, ""),
            delegation_id=values.get(DELEGATION_ID_ENV, ""),
            lease_person_id=values.get(LEASE_PERSON_ENV, ""),
            lease_run_id=values.get(LEASE_RUN_ENV, ""),
        )


_current_invocation: ContextVar[MemberInvocation | None] = ContextVar(
    "guildbotics_member_invocation", default=None
)
_empty_invocation = MemberInvocation()


def active_member_invocation() -> MemberInvocation | None:
    """Return the explicitly bound invocation, if one exists."""
    return _current_invocation.get()


def current_member_invocation() -> MemberInvocation:
    """Return the current invocation or an empty context outside member work."""
    return active_member_invocation() or _empty_invocation


@contextmanager
def member_invocation_scope(invocation: MemberInvocation) -> Iterator[None]:
    """Bind invocation metadata to the current asynchronous context."""
    token = _current_invocation.set(invocation)
    try:
        yield
    finally:
        _current_invocation.reset(token)
