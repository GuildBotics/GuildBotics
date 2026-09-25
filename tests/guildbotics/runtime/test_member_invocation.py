from __future__ import annotations

import asyncio

import pytest

from guildbotics.runtime.member_invocation import (
    CHAT_PARTICIPANT_LABELS_ENV,
    DELEGATION_ID_ENV,
    LEASE_ID_ENV,
    LEASE_PERSON_ENV,
    LEASE_RUN_ENV,
    RUN_ENV,
    TASK_RUN_ENV,
    TRACE_ID_ENV,
    MemberInvocation,
    current_member_invocation,
    member_invocation_scope,
)


def test_member_invocation_reads_every_boundary_value() -> None:
    invocation = MemberInvocation.from_environment(
        {
            RUN_ENV: "chat-run",
            TASK_RUN_ENV: "task-run",
            CHAT_PARTICIPANT_LABELS_ENV: '{"U1":"aiko"}',
            TRACE_ID_ENV: "trace-1",
            LEASE_ID_ENV: "lease-1",
            DELEGATION_ID_ENV: "delegation-1",
            LEASE_PERSON_ENV: "aiko",
            LEASE_RUN_ENV: "lease-run-1",
        }
    )

    assert invocation == MemberInvocation(
        run_id="chat-run",
        task_run_id="task-run",
        participant_labels='{"U1":"aiko"}',
        trace_id="trace-1",
        lease_id="lease-1",
        delegation_id="delegation-1",
        lease_person_id="aiko",
        lease_run_id="lease-run-1",
    )


@pytest.mark.asyncio
async def test_member_invocations_are_isolated_between_async_tasks() -> None:
    ready = (asyncio.Event(), asyncio.Event())
    observed: list[tuple[str, str]] = []

    async def read_invocation(index: int, run_id: str) -> None:
        with member_invocation_scope(MemberInvocation(run_id=run_id)):
            ready[index].set()
            await ready[1 - index].wait()
            invocation = current_member_invocation()
            observed.append((run_id, invocation.run_id))

    await asyncio.gather(read_invocation(0, "run-1"), read_invocation(1, "run-2"))

    assert sorted(observed) == [("run-1", "run-1"), ("run-2", "run-2")]
    assert current_member_invocation() == MemberInvocation()
