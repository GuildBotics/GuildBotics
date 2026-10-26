from __future__ import annotations

import asyncio

import pytest

from guildbotics.runtime.member_invocation import (
    ChatSubject,
    MemberInvocation,
    Work,
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


def test_a_chat_threads_work_names_its_channel_and_thread():
    """What a run resumed in a chat thread's work replies to is read back from
    the work as :meth:`Work.of_chat` keyed it; any other work names none."""
    work = Work.of_chat(ChatSubject("slack", "C1", "100.1", "E1", "U_BOT"))

    assert Work("chat", work.identity).chat_thread() == ("C1", "100.1")
    assert Work.of_ticket("https://example.test/1").chat_thread() == ("", "")
    assert Work("chat", "malformed").chat_thread() == ("", "")
