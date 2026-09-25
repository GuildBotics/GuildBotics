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
    a_entered, b_entered, a_read = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    observed: list[tuple[str, str]] = []

    async def task_a() -> None:
        with member_invocation_scope(MemberInvocation(run_id="run-1")):
            a_entered.set()
            await b_entered.wait()
            observed.append(("a", current_member_invocation().run_id))
            a_read.set()

    async def task_b() -> None:
        with member_invocation_scope(MemberInvocation(run_id="run-2")):
            await a_entered.wait()
            b_entered.set()
            await a_read.wait()
            observed.append(("b", current_member_invocation().run_id))

    await asyncio.gather(task_a(), task_b())

    assert sorted(observed) == [("a", "run-1"), ("b", "run-2")]


def test_member_invocation_scope_restores_the_default() -> None:
    with member_invocation_scope(MemberInvocation(run_id="run-1")):
        assert current_member_invocation().run_id == "run-1"

    assert current_member_invocation() == MemberInvocation()
