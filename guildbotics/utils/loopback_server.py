"""An ASGI app served on a free loopback port inside the running event loop."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from typing import Any

from uvicorn import Config, Server

LOOPBACK_HOST = "127.0.0.1"
_START_POLLS = 100
_START_POLL_SECONDS = 0.01
_STOP_TIMEOUT_SECONDS = 3.0


class _EmbeddedServer(Server):
    """Run uvicorn without replacing the application's signal handlers."""

    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class LoopbackServer:
    """One ASGI app on ``127.0.0.1`` at a port the OS chose, for its owner.

    It logs no requests: what passes through belongs to the owner.
    """

    def __init__(self, server: _EmbeddedServer, task: asyncio.Task[None], port: int):
        self._server = server
        self.task = task
        self.port = port

    @classmethod
    async def start(cls, make_app: Callable[[int], Any]) -> LoopbackServer:
        """Serve the app ``make_app`` builds for its port, once it accepts.

        Raises:
            RuntimeError: When the server does not start.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LOOPBACK_HOST, 0))
        port = int(sock.getsockname()[1])
        server = _EmbeddedServer(
            Config(
                make_app(port),
                host=LOOPBACK_HOST,
                port=port,
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=1,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(_START_POLLS):
                if server.started:
                    return cls(server, task, port)
                if task.done():
                    await task
                await asyncio.sleep(_START_POLL_SECONDS)
            raise RuntimeError("The loopback server did not start.")
        except BaseException:
            server.should_exit = True
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            sock.close()
            raise

    def stop_soon(self) -> None:
        """Stop accepting; the server finishes on its own shortly after."""
        self._server.should_exit = True

    async def stop(self) -> None:
        """Stop the server and wait for it, cancelling it if it lingers."""
        self.stop_soon()
        try:
            await asyncio.wait_for(self.task, timeout=_STOP_TIMEOUT_SECONDS)
        except TimeoutError:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
