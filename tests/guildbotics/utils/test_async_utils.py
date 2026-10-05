"""Cancellation never lets a caller leave its thread work behind."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from guildbotics.utils.async_utils import finish_on_cancel, to_thread


@pytest.mark.asyncio
async def test_thread_arguments_result_and_context() -> None:
    context = contextvars.ContextVar("thread-test", default="absent")
    token = context.set("caller")
    caller = threading.get_ident()

    def work(value: int, *, extra: int) -> tuple[int, str, bool]:
        return value + extra, context.get(), threading.get_ident() != caller

    try:
        assert await to_thread(work, 2, extra=3) == (5, "caller", True)
    finally:
        context.reset(token)


@pytest.mark.asyncio
async def test_thread_failure_reaches_the_caller() -> None:
    def fail() -> None:
        raise ValueError("worker failed")

    with pytest.raises(ValueError, match="worker failed"):
        await to_thread(fail)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_repeated_cancellation_waits_for_the_thread(fails: bool) -> None:
    started = asyncio.Event()
    release, finished = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def work() -> None:
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(10)
            if fails:
                raise ValueError("worker failed after cancellation")
        finally:
            finished.set()

    task = asyncio.create_task(to_thread(work))
    try:
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 5)
    assert finished.is_set()
    if fails:
        assert isinstance(caught.value.__cause__, ValueError)
        assert str(caught.value.__cause__) == "worker failed after cancellation"
    else:
        assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_cancellation_waits_for_a_write_queued_in_a_busy_executor(monkeypatch):
    release, finished = threading.Event(), threading.Event()
    submitted = asyncio.Event()
    loop = asyncio.get_running_loop()
    run_in_executor = loop.run_in_executor

    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(release.wait, 10)

        def submit(_executor, function):
            future = run_in_executor(executor, function)
            submitted.set()
            return future

        monkeypatch.setattr(loop, "run_in_executor", submit)
        task = asyncio.create_task(to_thread(finished.set))
        try:
            await asyncio.wait_for(submitted.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert not finished.is_set()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert blocker.result(timeout=5)
    assert finished.is_set()


@pytest.mark.asyncio
async def test_cancel_notification_precedes_worker_completion() -> None:
    work = asyncio.get_running_loop().create_future()
    notified = asyncio.Event()
    caller = asyncio.create_task(finish_on_cancel(work, on_cancel=notified.set))
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.wait_for(notified.wait(), 5)
    assert not work.done()
    assert not caller.done()
    work.set_result("finished")
    with pytest.raises(asyncio.CancelledError):
        await caller


@pytest.mark.asyncio
async def test_a_worker_cancelled_by_loop_shutdown_does_not_stop_settling() -> None:
    work = asyncio.get_running_loop().create_future()
    caller = asyncio.create_task(finish_on_cancel(work))
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, 5)
