"""An AI CLI turn, from inside the command's isolated environment.

A command runs in its microVM, and so do its turns: the adapter starts the
provider CLI as a process of its own there. What only the host holds -- the
login the turn is lent, the member broker its member commands go through --
the turn asks the host for through the command's window when it starts
(``begin_turn``), and gives back when it ends (``end_turn``). This is the one
module that starts a provider's process.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from guildbotics.intelligences.agent_runtime.host_client import (
    HostCallError,
    HostClient,
    HostTurn,
    command_window,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
)
from guildbotics.utils.log_utils import get_logger

#: How long the pipes of a process the turn started are read after it ended.
_PIPES_OUTLIVE_SECONDS = 2.0

_MEMBER_TOOL_INSTRUCTION = """<guildbotics_member_transport>
A trusted MCP tool named `guildbotics_member` is available. Use it for every
command documented as `guildbotics member ...`; never run those commands in the
terminal. Pass the exact CLI tokens after `member` as `arguments`, without shell
quoting. For any command documented with `--content-file`, pass `--content-stdin`
instead and put the exact UTF-8 content in the tool's `stdin` field. Set
`turn_grant` to `{turn_grant}`. This grant is valid only for this turn.
</guildbotics_member_transport>"""


class TurnError(RuntimeError):
    """A process of the turn could not be started, or a file of it could not
    be written or read."""


class ProviderProcess(Protocol):
    """A provider's process, as an adapter speaks to it: an asyncio
    subprocess whose ``kill`` waits for it to end."""

    stdin: Any
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    async def kill(self) -> None: ...

    async def communicate(self) -> tuple[bytes, bytes]: ...


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


class TurnProcess:
    """A process the turn started, with the surface adapters speak to.

    It ends with the process itself, not with what the process started: a
    process a provider leaves running may hold its pipes open past it, so
    they are closed shortly after it ends -- whoever reads them reaches
    their end then -- and what it left ends with the microVM.
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
        limit: int,
        cwd: str,
        env: Mapping[str, str],
    ) -> TurnProcess:
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

    async def communicate(self) -> tuple[bytes, bytes]:
        return await self._process.communicate()

    async def _close_once_ended(self, transport: asyncio.SubprocessTransport) -> None:
        try:
            await self.wait()
            await asyncio.sleep(_PIPES_OUTLIVE_SECONDS)
        finally:
            transport.close()


@dataclass(frozen=True, slots=True)
class TurnSpec:
    """The turn as its provider sees it: its working directory, home and
    environment, and the microVM's mounts, by where, and whether read-only."""

    cwd: str
    home: str
    env: Mapping[str, str]
    mounts: Mapping[str, bool]


@dataclass(frozen=True, slots=True)
class TurnBroker:
    """The member broker the turn's member commands go through: its MCP
    server, and the grant they carry, valid only for this turn."""

    name: str
    url: str
    authorization: str
    turn_grant: str

    @property
    def mcp_server(self) -> dict[str, Any]:
        """The ACP HTTP MCP server descriptor of the broker."""
        return {
            "type": "http",
            "name": self.name,
            "url": self.url,
            "headers": [{"name": "Authorization", "value": self.authorization}],
        }

    def prompt(self, prompt: str) -> str:
        """Prepend the member-tool contract of the turn to ``prompt``."""
        instruction = _MEMBER_TOOL_INSTRUCTION.format(turn_grant=self.turn_grant)
        return f"{instruction}\n\n{prompt}"


class Turn:
    """One turn, from its start to its end.

    ``spec`` is what the provider starts with, ``broker`` the member broker
    bound to the turn until it ends.
    """

    def __init__(
        self,
        client: HostClient,
        context: AgentExecutionContext,
        started: HostTurn,
        env: Mapping[str, str],
    ) -> None:
        self._client = client
        self._context = context
        self._ended = False
        self.spec = TurnSpec(
            cwd=started.cwd,
            home=started.home,
            env={**started.env, **env},
            mounts=started.mounts,
        )
        self.broker = TurnBroker(turn_grant=started.turn_grant, **started.member)

    async def run(self, command: str, *args: str, limit: int) -> TurnProcess:
        """Start ``command`` in the turn's working directory and environment.

        Raises:
            TurnError: When it cannot be started.
        """
        try:
            return await TurnProcess.start(
                command,
                *args,
                limit=limit,
                cwd=self.spec.cwd,
                # What the microVM runs everything with -- its PATH among it --
                # and the turn's own.
                env={**os.environ, "HOME": self.spec.home, **self.spec.env},
            )
        except OSError as exc:
            raise TurnError(f"Could not start {command}: {exc}") from exc

    async def write_file(self, path: str, data: bytes) -> None:
        """Write ``data`` at ``path``, making the directories it is in.

        Raises:
            TurnError: When it cannot be written.
        """
        try:
            await asyncio.to_thread(_write, Path(path), data)
        except OSError as exc:
            raise TurnError(f"Could not write {path}: {exc}") from exc

    async def read_file(self, path: str) -> bytes | None:
        """The file at ``path``, or None when there is none.

        Raises:
            TurnError: When it cannot be read.
        """
        try:
            return await asyncio.to_thread(Path(path).read_bytes)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TurnError(f"Could not read {path}: {exc}") from exc

    async def close(self) -> None:
        """End the turn, revoking what it was lent and its broker grant;
        idempotent. Why the login it was lent could not be used, if it could
        not, is the context's from then on (``context.login``). Ending the
        processes the turn started is the adapter's.

        A turn the host could not end stays what it was: the host ends it
        with the command, and refuses the command's next turn until then.
        """
        if self._ended:
            return
        self._ended = True
        try:
            refusal = await self._client.end_turn(self.broker.turn_grant)
        except HostCallError as exc:
            get_logger().warning("The host did not end the turn: %s", exc)
            return
        self._context.login.refusal = lambda: refusal


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def turn_window() -> HostClient:
    """The command's window to the host, which every AI CLI turn goes through.

    Raises:
        AgentRuntimeError: ``configuration`` outside a command's isolated
            environment, where no AI CLI turn runs.
    """
    client = command_window()
    if client is None:
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.CONFIGURATION,
            "An AI CLI turn runs only inside a command.",
        )
    return client


async def start_turn(
    context: AgentExecutionContext,
    tool_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> Turn:
    """Start a turn of ``tool_name`` working in ``context.cwd``.

    Args:
        context: The turn: its working directory and who it runs for.
        tool_name: The catalog name of the AI CLI tool.
        env: What the provider process starts with beyond what the host
            gives the turn: the tool's state, its gateway, the broker's token.

    Raises:
        AgentRuntimeError: ``configuration`` outside a command's environment,
            or what the host refused the turn with (the tool not logged in
            here, a working directory outside what the microVM mounted).
    """
    client = turn_window()
    started = await client.begin_turn(
        tool_name,
        str(context.cwd),
        run_id=context.run_id,
        conversation=context.conversation_key,
        participant_labels=context.participant_labels,
    )
    return Turn(client, context, started, env or {})
