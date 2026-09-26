"""What every native adapter does the same way around its provider.

An adapter holds, for one turn, the turn's environment and the provider it
started there, and ends both when the turn is over. The provider either runs
one process per turn that prints one JSON event per line (Claude Code,
Antigravity), or serves JSON-RPC for the turn (Codex App Server, the ACP
agents). Every turn is bounded by the adapter's deadline, and a turn that is
cut short or cancelled stops its provider before the error leaves the
adapter. What the provider says stays the adapter's to interpret.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

from guildbotics.intelligences.agent_environment.runtime import EnvironmentProcess
from guildbotics.intelligences.agent_runtime.environment import TurnEnvironment
from guildbotics.intelligences.agent_runtime.jsonrpc import LineJsonRpcTransport
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    AgentTerminalResult,
    ConversationRecord,
    EventSink,
)

_PROCESS_EXIT_GRACE_SECONDS = 2.0
_PIPE_DRAIN_TIMEOUT_SECONDS = 2.0


@asynccontextmanager
async def turn_deadline(
    label: str, timeout: float, interrupt: Callable[[], Awaitable[None]]
) -> AsyncIterator[None]:
    """Bound a turn's provider work by ``timeout`` seconds.

    A turn that runs out of time, or is cancelled, interrupts its provider
    first: the interrupt may still need the turn's identity, which the adapter
    forgets once the turn has left this block.

    Raises:
        AgentRuntimeError: ``process`` when the deadline passes.
    """
    try:
        async with asyncio.timeout(timeout):
            yield
    except TimeoutError as exc:
        await interrupt()
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.PROCESS,
            f"{label} turn timed out.",
            rotate_session=True,
        ) from exc
    except asyncio.CancelledError:
        await interrupt()
        raise


class ProviderAdapter:
    """The turn's environment, and the provider an adapter started in it."""

    def __init__(self, *, executable: str, timeout: float) -> None:
        self._executable = executable
        self._timeout = timeout
        self._environment: TurnEnvironment | None = None

    async def interrupt(self) -> None:
        """Stop the running turn: its member broker first, then its provider."""
        if self._environment is not None:
            await self._environment.broker.deactivate()
        await self._stop_provider()

    async def _stop_provider(self) -> None:
        raise NotImplementedError

    async def _close_environment(self) -> None:
        environment, self._environment = self._environment, None
        if environment is not None:
            await environment.close()


class StreamJsonAdapter(ProviderAdapter):
    """An adapter whose provider runs one process per turn.

    A subclass starts the turn's environment and process in
    ``_run_active_turn``, keeping them in ``_environment`` and ``_process``;
    nothing of the turn keeps running in the environment it shares once the
    turn is over.
    """

    def __init__(self, *, executable: str, timeout: float) -> None:
        super().__init__(executable=executable, timeout=timeout)
        self._process: EnvironmentProcess | None = None

    async def run_turn(
        self,
        prompt: str,
        context: AgentExecutionContext,
        conversation: ConversationRecord,
        emit: EventSink,
    ) -> AgentTerminalResult:
        try:
            return await self._run_active_turn(prompt, context, conversation, emit)
        finally:
            await self.close()

    async def _run_active_turn(
        self,
        prompt: str,
        context: AgentExecutionContext,
        conversation: ConversationRecord,
        emit: EventSink,
    ) -> AgentTerminalResult:
        raise NotImplementedError

    async def _stop_provider(self) -> None:
        if self._process is not None and self._process.returncode is None:
            await self._process.kill()

    async def close(self) -> None:
        try:
            await self.interrupt()
        finally:
            await self._close_environment()


class JsonRpcAdapter(ProviderAdapter):
    """An adapter whose provider serves JSON-RPC for the turn.

    The provider is started for each turn and stopped with it; a session
    outlives it by being resumed by id.

    Args:
        label: The provider as its transport errors name it.
        include_version: Whether the protocol requires ``"jsonrpc": "2.0"``.
    """

    def __init__(
        self,
        *,
        executable: str,
        timeout: float,
        label: str,
        include_version: bool = False,
    ) -> None:
        super().__init__(executable=executable, timeout=timeout)
        self._transport = LineJsonRpcTransport(
            label=label,
            include_version=include_version,
            request_timeout=min(timeout, 30.0),
            on_reverse_request=self._handle_reverse_request,
        )

    async def _stop_provider(self) -> None:
        await self._cancel_turn()
        await self._transport.kill()

    async def _cancel_turn(self) -> None:
        """Ask the provider to end the turn it is running, if any."""

    async def close(self) -> None:
        """Stop the provider and end its turn in the environment."""
        await self._transport.aclose()
        await self._close_environment()

    async def _handle_reverse_request(
        self, method: str, request_id: Any, _params: dict[str, Any]
    ) -> None:
        """Answer a request the provider sends; none is supported by default."""
        await self._transport.respond_unsupported(request_id, method)


class StreamJsonProcess:
    """A turn's provider process that prints one JSON event per line.

    Its stderr is collected from the start, so a provider that fills the
    pipe never blocks on it.
    """

    def __init__(self, process: EnvironmentProcess, label: str) -> None:
        self._process = process
        self._label = label
        self._stderr_task = asyncio.create_task(process.stderr.read())
        self.stderr = ""
        self._exit_status = 0

    async def next_event(self) -> dict[str, Any] | None:
        """The next JSON object the provider printed, or ``None`` at its end.

        Lines that hold anything but an object carry nothing for a turn and
        are skipped.

        Raises:
            AgentRuntimeError: ``protocol`` when a line is not JSON or cannot
                be read at all.
        """
        while True:
            try:
                line = await self._process.stdout.readline()
                if not line:
                    return None
                raw = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise AgentRuntimeError(
                    AgentRuntimeErrorCategory.PROTOCOL,
                    f"Malformed {self._label} stream-json event: {exc}",
                    rotate_session=True,
                ) from exc
            except ValueError as exc:  # A line longer than the stream limit.
                raise AgentRuntimeError(
                    AgentRuntimeErrorCategory.PROTOCOL,
                    f"{self._label} stream-json output could not be read: {exc}",
                    rotate_session=True,
                ) from exc
            if isinstance(raw, dict):
                return raw

    async def finish(self) -> None:
        """Let the process exit on its own briefly, then end it and its stderr."""
        process = self._process
        if process.returncode is None:
            with suppress(Exception):
                await asyncio.wait_for(
                    process.wait(), timeout=_PROCESS_EXIT_GRACE_SECONDS
                )
        observed = process.returncode
        await process.kill()
        self._exit_status = (
            observed if observed is not None else (process.returncode or 0)
        )
        try:
            stderr = await asyncio.wait_for(
                self._stderr_task, timeout=_PIPE_DRAIN_TIMEOUT_SECONDS
            )
        except TimeoutError:
            self._stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stderr_task
        else:
            self.stderr = stderr.decode(errors="replace").strip()

    def returncode(self, *, terminal_seen: bool) -> int:
        """The process's exit status as the turn's outcome, after :meth:`finish`.

        A terminal result the provider printed is authoritative. Any later
        negative exit status can be caused by ending a CLI that is still
        waiting for background descendants, and must not discard the valid
        response or rotate its resumable session.
        """
        return 0 if terminal_seen else self._exit_status
