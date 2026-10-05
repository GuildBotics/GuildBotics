"""Thread work that finishes before its cancelled caller leaves."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress


async def finish_on_cancel[T](
    work: asyncio.Future[T], *, on_cancel: Callable[[], None] | None = None
) -> T:
    """Wait for work even when the caller is cancelled, then propagate cancellation.

    ``on_cancel`` can ask the worker to stop at its next safe point. Repeated
    cancellation of the caller does not abandon the worker. If the loop itself
    cancels the worker task, its executor owns the thread's shutdown instead.

    Raises:
        asyncio.CancelledError: After work settles, with any worker failure
            retained as its cause.
        Exception: When work fails without the caller being cancelled.
    """
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError as cancelled:
        if on_cancel is not None:
            on_cancel()
        while not work.done():
            with suppress(asyncio.CancelledError):
                await asyncio.wait({work})
        if not work.cancelled():
            error = work.exception()
            if error is not None:
                raise cancelled from error
        raise


async def to_thread[**P, T](
    function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run a function off the loop, retaining its writes until it finishes.

    Arguments, context variables, results and failures follow asyncio.to_thread;
    cancellation waits for the thread before it reaches the caller.
    """
    return await finish_on_cancel(
        asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    )
