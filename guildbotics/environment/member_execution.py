"""Member command workers shared by the CLI API and the command broker."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any

from pydantic import BaseModel

from guildbotics.utils.async_utils import finish_on_cancel
from guildbotics.utils.process_limits import STREAM_READ_LIMIT

_MAX_OUTPUT_BYTES = STREAM_READ_LIMIT


class MemberCommandResult(BaseModel):
    """The exit code and streams of one member invocation."""

    exit_code: int
    stdout: str
    stderr: str


class MemberWorkers:
    """Run asyncio.run callers on clean threads, retaining timed-out work."""

    def __init__(self) -> None:
        self.workers: dict[asyncio.Future[tuple[int, str, str]], threading.Event] = {}

    def submit(
        self,
        run: Callable[[], tuple[int, str, str]],
        deadline: float,
        *,
        on_finished: Callable[[], None] = lambda: None,
    ) -> tuple[Coroutine[Any, Any, MemberCommandResult], threading.Event]:
        cancelled = threading.Event()

        def command() -> tuple[int, str, str]:
            if cancelled.is_set():
                return (
                    125,
                    "",
                    "Member capability command was cancelled before it started.",
                )
            return run()

        work = asyncio.get_running_loop().run_in_executor(
            None, contextvars.Context().run, command
        )
        self.workers[work] = cancelled
        work.add_done_callback(self._finished)
        work.add_done_callback(lambda _work: on_finished())

        async def answer() -> MemberCommandResult:
            try:
                code, stdout, stderr = await asyncio.wait_for(
                    asyncio.shield(work), max(0.0, deadline - time.monotonic())
                )
                return MemberCommandResult(
                    exit_code=code, stdout=_bounded(stdout), stderr=_bounded(stderr)
                )
            except TimeoutError:
                cancelled.set()
                return MemberCommandResult(
                    exit_code=124,
                    stdout="",
                    stderr="Member capability command timed out; it may still complete.",
                )
            except asyncio.CancelledError:
                cancelled.set()
                raise

        return answer(), cancelled

    def run(
        self,
        run: Callable[[], tuple[int, str, str]],
        deadline: float,
        *,
        on_finished: Callable[[], None] = lambda: None,
    ) -> Coroutine[Any, Any, MemberCommandResult]:
        return self.submit(run, deadline, on_finished=on_finished)[0]

    def _finished(self, work: asyncio.Future[tuple[int, str, str]]) -> None:
        self.workers.pop(work, None)
        if not work.cancelled():
            work.exception()

    async def settle(self) -> None:
        """Wait even through cancellation before dependencies are closed."""
        await finish_on_cancel(asyncio.gather(*self.workers, return_exceptions=True))


def _bounded(text: str) -> str:
    encoded = text.encode()
    if len(encoded) <= _MAX_OUTPUT_BYTES:
        return text
    return encoded[:_MAX_OUTPUT_BYTES].decode(errors="ignore") + "\n[output truncated]"
