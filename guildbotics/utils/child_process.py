"""A process started with piped standard streams, which ends with itself."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress

#: How long the pipes of a process are read after it ended.
_PIPES_OUTLIVE_SECONDS = 2.0

#: asyncio's own buffer limit of a stream reader.
_STREAM_LIMIT = 1 << 16


class _Protocol(asyncio.subprocess.SubprocessStreamProtocol):
    """asyncio's protocol for a process with piped streams, which also says
    when the process itself has ended (``ended``) -- asyncio's own ``wait``
    says so only once its pipes have closed too."""

    def __init__(self, limit: int, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(limit, loop)
        self.ended: asyncio.Future[None] = loop.create_future()

    def process_exited(self) -> None:
        super().process_exited()
        if not self.ended.done():
            self.ended.set_result(None)


class ChildProcess:
    """A process with piped standard streams.

    It ends with the process itself, not with what the process started: a
    process it leaves running may hold its pipes open past it, so they are
    closed shortly after it ends -- whoever reads them reaches their end
    then -- and what it left ends with the microVM it runs in.
    """

    def __init__(
        self, transport: asyncio.SubprocessTransport, protocol: _Protocol
    ) -> None:
        self._ended = protocol.ended
        self._process = asyncio.subprocess.Process(
            transport, protocol, asyncio.get_running_loop()
        )
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self.stdin: asyncio.StreamWriter = self._process.stdin
        self.stdout: asyncio.StreamReader = self._process.stdout
        self.stderr: asyncio.StreamReader = self._process.stderr
        self._closing = asyncio.create_task(self._close_once_ended(transport))

    @classmethod
    async def start(
        cls,
        command: str,
        *args: str,
        cwd: str,
        env: Mapping[str, str],
        limit: int = _STREAM_LIMIT,
    ) -> ChildProcess:
        """Start ``command`` with piped standard streams.

        Raises:
            OSError: When it cannot be started.
        """
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.subprocess_exec(
            lambda: _Protocol(limit, loop),
            command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        return cls(transport, protocol)

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def wait(self) -> int:
        """Wait until the process itself has ended."""
        await asyncio.shield(self._ended)
        returncode = self._process.returncode
        assert returncode is not None
        return returncode

    async def kill(self) -> None:
        """End the process now, and wait until it has."""
        if self._process.returncode is None:
            with suppress(ProcessLookupError):
                self._process.kill()
        await self.wait()

    async def communicate(self, stdin: bytes = b"") -> tuple[bytes, bytes]:
        """Write ``stdin`` and close it, then read both outputs to their end,
        which comes with the process itself (see the class)."""
        stdout = asyncio.create_task(self.stdout.read())
        stderr = asyncio.create_task(self.stderr.read())
        try:
            with suppress(BrokenPipeError, ConnectionResetError):
                self.stdin.write(stdin)
                await self.stdin.drain()
            self.stdin.close()
            await self.wait()
            return await stdout, await stderr
        finally:
            stdout.cancel()
            stderr.cancel()

    async def _close_once_ended(self, transport: asyncio.SubprocessTransport) -> None:
        try:
            await self.wait()
            await asyncio.sleep(_PIPES_OUTLIVE_SECONDS)
        finally:
            transport.close()
