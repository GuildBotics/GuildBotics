from __future__ import annotations

import asyncio

import pytest

from guildbotics.runtime.member_invocation import (
    MemberInvocation,
    current_member_invocation,
    member_invocation_scope,
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
