"""Explicit ordering for tests of a running member command's timeout."""

import asyncio
import time

import pytest


@pytest.fixture
def start_member_before_timeout(monkeypatch):
    """Submit one command and observe its worker before returning the Future.

    The worker signals a threading.Event directly: the event loop cannot
    process a callback while submission waits for that signal.
    """

    async def invoke(started, operation, delay):
        loop = asyncio.get_running_loop()
        submit = loop.run_in_executor
        with monkeypatch.context() as patch:

            def starting(executor, function, *args):
                # Only this submission is ordered; later work keeps its timing.
                patch.setattr(loop, "run_in_executor", submit)

                def delayed():
                    time.sleep(delay)
                    return function(*args)

                work = submit(executor, delayed)
                assert started.wait(10), "the member worker did not start"
                return work

            patch.setattr(loop, "run_in_executor", starting)
            return await operation

    return invoke
