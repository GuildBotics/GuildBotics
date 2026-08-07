"""Turn-scoped MCP transport for trusted member capabilities.

The native agent stays inside its provider sandbox. This broker runs beside the
agent in the GuildBotics process and exposes exactly one authenticated tool
which launches the fixed ``guildbotics member`` entrypoint without a shell.
Provider credentials therefore remain in the member CLI's trusted process.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import sys
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl, BaseModel
from uvicorn import Config, Server

from guildbotics.intelligences.agent_runtime.environment import (
    STREAM_READ_LIMIT,
    member_command_environment,
    terminate_process_tree,
)
from guildbotics.intelligences.agent_runtime.models import AgentExecutionContext
from guildbotics.runtime.person_lease import (
    DELEGATION_ID_ENV,
    LEASE_ID_ENV,
    LEASE_PERSON_ENV,
    LEASE_RUN_ENV,
)

_HOST = "127.0.0.1"
_SCOPE = "member:execute"
_MAX_ARGUMENTS = 128
_MAX_ARGUMENT_BYTES = 64 * 1024
_MAX_STDIN_BYTES = 2 * 1024 * 1024
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_MAX_OUTPUT_BYTES = STREAM_READ_LIMIT
_COMMAND_TIMEOUT_SECONDS = 300.0


class MemberCapabilityBrokerError(RuntimeError):
    """Report a trusted broker lifecycle failure to the native adapter."""


class MemberCommandResult(BaseModel):
    """Structured result returned to the native agent."""

    exit_code: int
    stdout: str
    stderr: str


class _ScopedTokenVerifier(TokenVerifier):
    """Verify one unguessable token without leaking timing information."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id="guildbotics-native-agent",
            scopes=[_SCOPE],
        )


class _EmbeddedServer(Server):
    """Run uvicorn without replacing the application's signal handlers."""

    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class MemberCapabilityBroker:
    """Expose the active turn's member CLI through authenticated localhost MCP."""

    def __init__(self, command: tuple[str, ...] | None = None) -> None:
        self._command = command or _member_cli_command()
        self._token = secrets.token_urlsafe(32)
        self._turn_grant = ""
        self._context: AgentExecutionContext | None = None
        self._command_lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._server: _EmbeddedServer | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._url = ""

    @property
    def mcp_server(self) -> dict[str, Any]:
        """Return the ACP HTTP MCP server descriptor for this broker."""
        if not self._url:
            raise RuntimeError("Member capability broker is not running.")
        return {
            "type": "http",
            "name": "guildbotics-member",
            "url": self._url,
            "headers": [{"name": "Authorization", "value": f"Bearer {self._token}"}],
        }

    @property
    def turn_grant(self) -> str:
        """Return the opaque grant required by calls in the active turn."""
        if not self._turn_grant:
            raise RuntimeError("Member capability broker has no active turn.")
        return self._turn_grant

    async def activate(self, context: AgentExecutionContext) -> None:
        """Start the broker if needed and bind it to one active turn."""
        if self._context is not None:
            raise MemberCapabilityBrokerError(
                "Member capability broker already has an active turn."
            )
        if self._serve_task is None:
            try:
                await self._start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise MemberCapabilityBrokerError(
                    "Member capability broker could not start."
                ) from exc
        elif self._serve_task.done():
            if self._serve_task.cancelled():
                cause = None
            else:
                cause = self._serve_task.exception()
            error = MemberCapabilityBrokerError(
                "Member capability broker stopped unexpectedly."
            )
            if cause is None:
                raise error
            raise error from cause
        self._context = context
        self._turn_grant = secrets.token_urlsafe(24)

    async def deactivate(self, context: AgentExecutionContext) -> None:
        """Revoke command execution after the matching turn finishes."""
        if self._context is context:
            self._context = None
            self._turn_grant = ""
            process = self._process
            if process is not None and process.returncode is None:
                await terminate_process_tree(process)

    async def execute(
        self, turn_grant: str, arguments: list[str], stdin: str = ""
    ) -> MemberCommandResult:
        """Run one member command for the active person and workspace."""
        encoded_stdin = stdin.encode()
        if len(encoded_stdin) > _MAX_STDIN_BYTES:
            raise ValueError("Member command stdin is too large.")
        async with self._command_lock:
            context = self._context
            if context is None:
                raise ValueError("No GuildBotics turn is active.")
            if not secrets.compare_digest(turn_grant, self._turn_grant):
                raise ValueError("The GuildBotics turn grant is invalid or expired.")
            _validate_arguments(arguments, context.person_id)
            argv = (
                *self._command,
                "member",
                "--workspace",
                str(context.workspace_root),
                *arguments,
            )
            env = _member_environment(context)
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(context.cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=STREAM_READ_LIMIT,
            )
            self._process = process
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(encoded_stdin),
                    timeout=_COMMAND_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                await terminate_process_tree(process)
                return MemberCommandResult(
                    exit_code=124,
                    stdout="",
                    stderr="Member capability command timed out.",
                )
            except BaseException:
                if process.returncode is None:
                    await terminate_process_tree(process)
                raise
            finally:
                self._process = None
        return MemberCommandResult(
            exit_code=int(process.returncode or 0),
            stdout=_decode_output(stdout),
            stderr=_decode_output(stderr),
        )

    async def close(self) -> None:
        """Revoke the token and stop the loopback server."""
        self._context = None
        self._turn_grant = ""
        process = self._process
        if process is not None and process.returncode is None:
            await terminate_process_tree(process)
        server, task = self._server, self._serve_task
        self._server = None
        self._serve_task = None
        self._url = ""
        self._token = secrets.token_urlsafe(32)
        if server is None or task is None:
            return
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((_HOST, 0))
        port = int(sock.getsockname()[1])
        origin = f"http://{_HOST}:{port}"
        url = f"{origin}/mcp"
        mcp = MCPServer(
            "GuildBotics Member",
            instructions=(
                "Use guildbotics_member for every command documented as "
                "`guildbotics member ...`. Pass only the arguments after `member`."
            ),
            token_verifier=_ScopedTokenVerifier(self._token),
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(origin),
                resource_server_url=AnyHttpUrl(url),
                required_scopes=[_SCOPE],
            ),
        )

        @mcp.tool(name="guildbotics_member", structured_output=True)
        async def guildbotics_member(
            turn_grant: str, arguments: list[str], stdin: str = ""
        ) -> MemberCommandResult:
            """Run a trusted `guildbotics member` capability.

            Pass command tokens after `guildbotics member` in ``arguments`` and
            include the active prompt's ``turn_grant``. Pass content for
            ``--content-stdin`` in ``stdin``. Never include shell quoting,
            redirects, heredocs, `guildbotics`, or `member` itself.
            """
            return await self.execute(turn_grant, arguments, stdin)

        app = mcp.streamable_http_app(
            stateless_http=True,
            max_request_body_size=_MAX_REQUEST_BYTES,
        )
        server = _EmbeddedServer(
            Config(
                app,
                host=_HOST,
                port=port,
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=1,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(100):
                if server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("Member capability broker did not start.")
        except BaseException:
            server.should_exit = True
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            sock.close()
            raise
        self._server = server
        self._serve_task = task
        self._url = url


def _member_cli_command() -> tuple[str, ...]:
    """Resolve the matching CLI entrypoint for source and packaged runtimes."""
    if not getattr(sys, "frozen", False):
        return (sys.executable, "-m", "guildbotics.cli")
    executable = Path.home() / ".guildbotics" / "bin" / "guildbotics"
    if not executable.is_file():
        raise RuntimeError(f"Bundled GuildBotics member CLI is missing: {executable}")
    return (str(executable),)


def _member_environment(context: AgentExecutionContext) -> dict[str, str]:
    env = os.environ.copy()
    env.update(member_command_environment(context))
    if context.lease_id and context.delegation_id:
        env.update(
            {
                LEASE_ID_ENV: context.lease_id,
                DELEGATION_ID_ENV: context.delegation_id,
                LEASE_PERSON_ENV: context.person_id,
                LEASE_RUN_ENV: context.run_id,
            }
        )
    else:
        for key in (LEASE_ID_ENV, DELEGATION_ID_ENV, LEASE_PERSON_ENV, LEASE_RUN_ENV):
            env.pop(key, None)
    return env


def _validate_arguments(arguments: list[str], person_id: str) -> None:
    if not arguments:
        raise ValueError("Member command arguments must not be empty.")
    if len(arguments) > _MAX_ARGUMENTS:
        raise ValueError("Member command has too many arguments.")
    if sum(len(value.encode()) for value in arguments) > _MAX_ARGUMENT_BYTES:
        raise ValueError("Member command arguments are too large.")
    if any("\0" in value for value in arguments):
        raise ValueError("Member command arguments must not contain NUL bytes.")
    if any(
        value == "--workspace" or value.startswith("--workspace=")
        for value in arguments
    ):
        raise ValueError("The member capability workspace cannot be overridden.")
    people: list[str] = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--person":
            if index + 1 >= len(arguments):
                raise ValueError("--person requires the active member ID.")
            people.append(arguments[index + 1])
            index += 2
            continue
        if value.startswith("--person="):
            people.append(value.partition("=")[2])
        index += 1
    if people and any(value != person_id for value in people):
        raise ValueError("Member capabilities cannot act as another person.")
    if not people and arguments != ["help"]:
        raise ValueError("Member commands must name the active person with --person.")


def _decode_output(value: bytes) -> str:
    if len(value) > _MAX_OUTPUT_BYTES:
        value = value[:_MAX_OUTPUT_BYTES]
        suffix = "\n[output truncated]"
    else:
        suffix = ""
    return value.decode(errors="replace") + suffix
