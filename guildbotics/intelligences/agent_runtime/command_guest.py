"""GuildBotics' own processes in a command's microVM, for its member commands.

The member broker runs a turn's member commands on worker threads of its
own, while the microVM they have to run in belongs to the command's event
loop. :class:`EnvironmentGuest` hands each process over to that loop and
waits for it within what the broker gives the command, so a command the
broker gave up on neither keeps its thread nor leaves its process running.
It takes no lock: it is called in the middle of a turn, which holds the
microVM for as long as it runs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext, suppress
from pathlib import Path

from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironment,
    AgentEnvironmentError,
    EnvironmentProcess,
    EnvironmentStdin,
)
from guildbotics.intelligences.agent_environment.spec import guest_path
from guildbotics.runtime.member_invocation import GuestProcessError, GuestResult

#: How much is read or written at a time.
_CHUNK_BYTES = 1 << 16
#: How much of a process's standard error is kept for its reason.
_MAX_STDERR_BYTES = 1 << 16


class EnvironmentGuest:
    """The processes of one command's microVM, run from any thread."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        environment: Callable[[], AgentEnvironment | None],
        deadline: float = 0.0,
    ) -> None:
        """
        Args:
            loop: The command's event loop, which the microVM belongs to.
            environment: The command's microVM, once a turn has booted it.
            deadline: When, on :func:`time.monotonic`, the invocation's time
                is up; until one is given (:meth:`until`), it runs nothing.
        """
        self._loop = loop
        self._environment = environment
        self._deadline = deadline

    def until(self, deadline: float) -> EnvironmentGuest:
        """The same microVM, for an invocation whose time is up at ``deadline``."""
        return EnvironmentGuest(self._loop, self._environment, deadline)

    def path(self, host: Path) -> str:
        return guest_path(host)

    def remaining(self) -> float:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise GuestProcessError("The member command ran out of time.")
        return left

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin: bytes | Path = b"",
        stdout: Path | None = None,
        stdout_limit: int,
    ) -> GuestResult:
        remaining = self.remaining()
        running = None
        with suppress(RuntimeError):
            running = asyncio.get_running_loop()
        if running is self._loop:
            # Waiting here would stop the loop the process has to run on.
            raise GuestProcessError(
                "The command's environment is run from another thread than"
                " its own loop's."
            )
        coroutine = self._run(tuple(argv), cwd, dict(env), stdin, stdout, stdout_limit)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except RuntimeError as exc:
            coroutine.close()
            raise GuestProcessError("The command's environment has ended.") from exc
        try:
            return future.result(timeout=remaining)
        except TimeoutError as exc:
            # Cancelling the task on the loop kills the process inside.
            future.cancel()
            raise GuestProcessError("The member command ran out of time.") from exc

    async def _run(
        self,
        argv: tuple[str, ...],
        cwd: str,
        env: dict[str, str],
        stdin: bytes | Path,
        stdout: Path | None,
        stdout_limit: int,
    ) -> GuestResult:
        environment = self._environment()
        if environment is None:
            raise GuestProcessError("No environment runs for this command.")
        try:
            process = await environment.run(
                argv[0],
                *argv[1:],
                limit=_CHUNK_BYTES,
                cwd=cwd,
                # The host's facts every environment is told, and nothing of
                # the turn's: its variables, the broker's token, the stand-ins.
                env={**environment.spec.env, **env},
            )
        except AgentEnvironmentError as exc:
            raise GuestProcessError(str(exc)) from exc
        feeding = asyncio.create_task(_feed(process.stdin, stdin))
        errors = asyncio.create_task(_head(process))
        try:
            out = await _read(process, stdout, stdout_limit)
            returncode = await process.wait()
            await feeding
            return GuestResult(returncode, out, await errors)
        except BaseException:
            feeding.cancel()
            errors.cancel()
            await process.kill()
            raise


async def _feed(sink: EnvironmentStdin, source: bytes | Path) -> None:
    """Write ``source`` to the process and close its input; a process that
    ends before reading all of it says with its exit status how that went."""
    try:
        if isinstance(source, bytes):
            sink.write(source)
            await sink.drain()
        else:
            with source.open("rb") as file:
                while chunk := file.read(_CHUNK_BYTES):
                    sink.write(chunk)
                    await sink.drain()
    except ConnectionError:
        pass
    finally:
        sink.close()


async def _read(process: EnvironmentProcess, sink: Path | None, limit: int) -> bytes:
    """The process's output, or streamed into ``sink``; never more than ``limit``."""
    kept = bytearray()
    written = 0
    with sink.open("wb") if sink is not None else nullcontext() as file:
        while chunk := await process.stdout.read(_CHUNK_BYTES):
            written += len(chunk)
            if written > limit:
                raise GuestProcessError(
                    f"The command wrote more than {limit} bytes of output."
                )
            if file is None:
                kept += chunk
            else:
                file.write(chunk)
    return bytes(kept)


async def _head(process: EnvironmentProcess) -> bytes:
    """The beginning of the process's standard error, read to its end."""
    kept = bytearray()
    while chunk := await process.stderr.read(_CHUNK_BYTES):
        kept += chunk[: _MAX_STDERR_BYTES - len(kept)]
    return bytes(kept)
