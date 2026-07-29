"""Line-delimited JSON-RPC transport shared by native agent adapters.

The transport owns framing and correlation only: request identifiers, pending
futures, line reading and writing, stderr collection, and the dispatch of
responses, notifications, and reverse requests. Provider method names, error
categories, session state, and approval decisions stay in the adapters, and the
process itself is created and terminated by the adapter that owns its sandbox
and credentials.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from guildbotics.intelligences.agent_runtime.models import (
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
)

METHOD_NOT_FOUND = -32601
FATAL_NOTIFICATION = "guildbotics/fatal"

_MAX_STDERR_LINES = 100
_MAX_STDERR_LINE = 8192

#: Called with ``(method, request_id, params)`` when the peer sends a request of
#: its own. The handler decides the answer and calls :meth:`respond`.
ReverseRequestHandler = Callable[[str, Any, dict[str, Any]], Awaitable[None]]


class _DefaultTimeout:
    """Sentinel for "use the transport's configured request timeout"."""


DEFAULT_TIMEOUT = _DefaultTimeout()


class RpcError(RuntimeError):
    """A JSON-RPC ``error`` object returned by the peer."""

    def __init__(self, error: Any) -> None:
        super().__init__(str(error))
        self.error = error


class LineJsonRpcTransport:
    """Frame JSON-RPC messages over a subprocess' stdin/stdout lines.

    Args:
        label: Human-readable peer name used in transport error messages.
        include_version: Whether outgoing messages carry ``"jsonrpc": "2.0"``.
            The Codex App Server omits it; the Agent Client Protocol requires it.
        request_timeout: Seconds to wait for a single request's response.
        on_reverse_request: Handler for peer-initiated requests. When omitted,
            every reverse request is answered with ``method not found``.
    """

    def __init__(
        self,
        *,
        label: str,
        include_version: bool = False,
        request_timeout: float = 30.0,
        on_reverse_request: ReverseRequestHandler | None = None,
    ) -> None:
        self._label = label
        self._include_version = include_version
        self._request_timeout = request_timeout
        self._on_reverse_request = on_reverse_request
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._request_id = 0
        self._stderr: list[str] = []
        self._fatal_error: AgentRuntimeError | None = None

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        return self._process

    @property
    def fatal_error(self) -> AgentRuntimeError | None:
        return self._fatal_error

    @property
    def running(self) -> bool:
        """Whether the peer process and its reader are both still healthy."""
        return (
            self._process is not None
            and self._process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
            and self._fatal_error is None
        )

    def start(self, process: asyncio.subprocess.Process) -> None:
        """Adopt a freshly spawned process and begin reading its streams."""
        self._process = process
        self._fatal_error = None
        self._stderr.clear()
        self._pending.clear()
        self._notifications = asyncio.Queue()
        self._request_id = 0
        self._reader_task = asyncio.create_task(self._read_messages())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def aclose(self) -> None:
        """Stop the reader tasks. The caller stops the process itself."""
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._reader_task = None
        self._stderr_task = None
        self._process = None

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None | _DefaultTimeout = DEFAULT_TIMEOUT,
    ) -> Any:
        """Send a request and wait for its correlated response.

        Args:
            method: The peer method to call.
            params: The request parameters.
            timeout: Seconds to wait, ``None`` to wait indefinitely, or the
                default sentinel to use the transport's configured timeout.
                A protocol whose response marks the end of a whole turn needs
                ``None``; the caller bounds that work itself.
        """
        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        effective = (
            self._request_timeout if isinstance(timeout, _DefaultTimeout) else timeout
        )
        try:
            await self.write({"method": method, "id": request_id, "params": params})
            return await asyncio.wait_for(future, timeout=effective)
        except TimeoutError as exc:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                f"{self._label} request '{method}' timed out.",
                rotate_session=True,
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.write({"method": method, "params": params})

    async def respond(
        self,
        request_id: Any,
        *,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Answer a reverse request with either a result or an error object."""
        message: dict[str, Any] = {"id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        await self.write(message)

    async def next_notification(self) -> dict[str, Any]:
        return await self._notifications.get()

    def drain_notifications(self) -> list[dict[str, Any]]:
        """Take every notification already queued, without waiting for more."""
        drained: list[dict[str, Any]] = []
        while True:
            try:
                drained.append(self._notifications.get_nowait())
            except asyncio.QueueEmpty:
                return drained

    def push_notification(self, message: dict[str, Any]) -> None:
        """Inject a synthetic notification into the adapter's message stream."""
        self._notifications.put_nowait(message)

    def stderr_text(self) -> str:
        return "\n".join(self._stderr)

    def stderr_tail(self, lines: int = 5) -> str:
        return "\n".join(self._stderr[-lines:])

    async def write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                f"{self._label} is not running.",
                rotate_session=True,
            )
        if self._include_version:
            message = {"jsonrpc": "2.0", **message}
        try:
            process.stdin.write(
                (
                    json.dumps(message, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                ).encode()
            )
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                f"{self._label} connection closed while sending a request.",
                rotate_session=True,
            ) from exc

    async def _read_messages(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while line := await process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    continue
                response_id = message.get("id")
                if response_id is not None and (
                    "result" in message or "error" in message
                ):
                    self._resolve(response_id, message)
                    continue
                if response_id is not None:
                    await self._dispatch_reverse_request(message)
                    continue
                self._notifications.put_nowait(message)
        except ValueError as exc:
            # Covers oversized readline() chunks and malformed JSON alike.
            self._fatal_error = AgentRuntimeError(
                AgentRuntimeErrorCategory.PROTOCOL,
                f"{self._label} output could not be read: {exc}",
                rotate_session=True,
            )
        finally:
            if self._fatal_error is None:
                self._fatal_error = AgentRuntimeError(
                    AgentRuntimeErrorCategory.PROCESS,
                    f"{self._label} stdout closed unexpectedly.",
                    details={
                        "returncode": process.returncode,
                        "stderr": self.stderr_tail(),
                    },
                    rotate_session=True,
                )
            self._fail_pending(self._fatal_error)
            self._notifications.put_nowait({"method": FATAL_NOTIFICATION})

    def _resolve(self, response_id: Any, message: dict[str, Any]) -> None:
        try:
            future = self._pending.get(int(response_id))
        except (TypeError, ValueError):
            return
        if future is None or future.done():
            return
        if "error" in message:
            future.set_exception(RpcError(message["error"]))
        else:
            future.set_result(message.get("result"))

    async def _dispatch_reverse_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        request_id = message.get("id")
        params = message.get("params")
        if self._on_reverse_request is not None:
            await self._on_reverse_request(
                method, request_id, params if isinstance(params, dict) else {}
            )
            return
        await self.respond(
            request_id,
            error={
                "code": METHOD_NOT_FOUND,
                "message": f"Unsupported request: {method}",
            },
        )

    async def _drain_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while line := await process.stderr.readline():
            text = line.decode(errors="replace").rstrip()
            if text:
                self._stderr.append(text[:_MAX_STDERR_LINE])
                del self._stderr[:-_MAX_STDERR_LINES]

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
