"""Turn-scoped MCP transport for trusted member capabilities, and the
command's window to the host.

The native agent stays inside its provider sandbox. This broker runs beside the
agent in the GuildBotics process and exposes exactly one authenticated tool
which runs the fixed ``guildbotics member`` commands in that process, without a
shell. Provider credentials therefore remain in the trusted host process.

The same server, with the same token, answers what the command's isolated
environment asks of the host (``POST /host/<call>``, JSON): the calls the
command's grant (:meth:`MemberCapabilityBroker.serve`) answers, in the
command's own context.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from guildbotics.intelligences.agent_environment.spec import GUEST_HOST_ALIAS
from guildbotics.intelligences.agent_runtime.command_guest import EnvironmentGuest
from guildbotics.intelligences.agent_runtime.host_client import HostCallError
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    AgentRuntimeError,
)
from guildbotics.runtime.member_invocation import MemberInvocation
from guildbotics.runtime.person_lease import PersonExecutionLease
from guildbotics.utils.loopback_server import LOOPBACK_HOST, LoopbackServer
from guildbotics.utils.process_limits import STREAM_READ_LIMIT

_SCOPE = "member:execute"
_MAX_ARGUMENTS = 128
_MAX_ARGUMENT_BYTES = 64 * 1024
_MAX_STDIN_BYTES = 2 * 1024 * 1024
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_MAX_OUTPUT_BYTES = STREAM_READ_LIMIT
_COMMAND_TIMEOUT_SECONDS = 300.0
MEMBER_BROKER_TOKEN_ENV = "GUILDBOTICS_MEMBER_BROKER_TOKEN"
#: The names a request's Host header may give this server: loopback, and the
#: alias the agent environment reaches the host by.
_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]", GUEST_HOST_ALIAS)

#: What answers the command's calls: the call's name and its arguments, to
#: its JSON result. It raises :class:`HostCallError` for a call it refuses.
HostCalls = Callable[[str, dict[str, Any]], Coroutine[Any, Any, Any]]

_MEMBER_TOOL_INSTRUCTION = """<guildbotics_member_transport>
A trusted MCP tool named `guildbotics_member` is available. Use it for every
command documented as `guildbotics member ...`; never run those commands in the
terminal. Pass the exact CLI tokens after `member` as `arguments`, without shell
quoting. For any command documented with `--content-file`, pass `--content-stdin`
instead and put the exact UTF-8 content in the tool's `stdin` field. Set
`turn_grant` to `{turn_grant}`. This grant is valid only for this turn.
</guildbotics_member_transport>"""


class MemberCapabilityBrokerError(RuntimeError):
    """Report a trusted broker lifecycle failure to the native adapter."""


class MemberCommandResult(BaseModel):
    """Structured result returned to the native agent."""

    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class MemberBrokerEndpoint:
    """Provider-neutral connection details for the trusted MCP endpoint.

    ``url`` reaches the broker from the host; ``guest_url`` reaches it from
    inside the agent environment, whose policy opens ``port`` to the guest.
    ``host_url`` and ``guest_host_url`` are the command's window to the host,
    reached with the same ``token``.
    """

    name: str
    url: str
    guest_url: str
    host_url: str
    guest_host_url: str
    port: int
    token: str

    @property
    def authorization(self) -> str:
        """The header value every request carries."""
        return f"Bearer {self.token}"


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


class MemberCapabilityBroker:
    """Expose the active turn's member CLI through authenticated localhost MCP.

    ``guest`` is the microVM of the command the broker serves: each command
    it runs is handed it, with the time the broker gives it, to run there what
    the command's turns can write.
    """

    def __init__(self, guest: EnvironmentGuest | None = None) -> None:
        self._guest = guest
        self._token = secrets.token_urlsafe(32)
        self._name = f"guildbotics-member-{secrets.token_hex(6)}"
        self._turn_grant = ""
        self._context: AgentExecutionContext | None = None
        self._command_lock = asyncio.Lock()
        self._host: HostCalls | None = None
        self._host_context = contextvars.Context()
        #: The calls of the command's environment being answered now.
        self._calls: set[asyncio.Task[Any]] = set()
        self._server: LoopbackServer | None = None
        self._url = ""
        self._port = 0

    @property
    def name(self) -> str:
        """Return the stable per-process MCP server name."""
        return self._name

    @property
    def endpoint(self) -> MemberBrokerEndpoint:
        """Return connection details adapters serialize for their MCP client."""
        if not self._url:
            raise RuntimeError("Member capability broker is not running.")
        host_url = self._url.removesuffix("/mcp") + "/host"
        return MemberBrokerEndpoint(
            name=self._name,
            url=self._url,
            guest_url=_as_guest(self._url),
            host_url=host_url,
            guest_host_url=_as_guest(host_url),
            port=self._port,
            token=self._token,
        )

    @property
    def mcp_server(self) -> dict[str, Any]:
        """Return the ACP HTTP MCP server descriptor for this broker.

        The provider reads it inside the agent environment, so the broker is
        named the way the guest reaches it.
        """
        endpoint = self.endpoint
        return {
            "type": "http",
            "name": endpoint.name,
            "url": endpoint.guest_url,
            "headers": [{"name": "Authorization", "value": endpoint.authorization}],
        }

    @property
    def turn_grant(self) -> str:
        """Return the opaque grant required by calls in the active turn."""
        if not self._turn_grant:
            raise RuntimeError("Member capability broker has no active turn.")
        return self._turn_grant

    def prompt(self, prompt: str) -> str:
        """Prepend the common member-tool contract for the active turn."""
        instruction = _MEMBER_TOOL_INSTRUCTION.format(turn_grant=self.turn_grant)
        return f"{instruction}\n\n{prompt}"

    def provider_environment(self) -> dict[str, str]:
        """Return the bearer token source required by provider MCP clients."""
        return {MEMBER_BROKER_TOKEN_ENV: self._token}

    def serve(self, host: HostCalls, context: contextvars.Context) -> None:
        """Answer the command's calls with ``host`` until the broker closes.

        Each call runs in a copy of ``context``, the command's own: unlike a
        member command, which starts from an empty one, a call is part of the
        command that makes it.
        """
        self._host = host
        self._host_context = context

    async def activate(self, context: AgentExecutionContext) -> None:
        """Start the broker if needed and bind it to one active turn."""
        if self._context is not None:
            raise MemberCapabilityBrokerError(
                "Member capability broker already has an active turn."
            )
        await self.start()
        self._context = context
        self._turn_grant = secrets.token_urlsafe(24)

    async def start(self) -> None:
        """Start the broker unless it runs.

        Raises:
            MemberCapabilityBrokerError: When it does not start, or stopped.
        """
        if self._server is None:
            try:
                await self._start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise MemberCapabilityBrokerError(
                    "Member capability broker could not start."
                ) from exc
        elif self._server.task.done():
            if self._server.task.cancelled():
                cause = None
            else:
                cause = self._server.task.exception()
            error = MemberCapabilityBrokerError(
                "Member capability broker stopped unexpectedly."
            )
            if cause is None:
                raise error
            raise error from cause

    async def deactivate(self, context: AgentExecutionContext | None = None) -> None:
        """Revoke command execution after the matching turn finishes."""
        if context is None or self._context is context:
            self._context = None
            self._turn_grant = ""

    async def execute(
        self, turn_grant: str, arguments: list[str], stdin: str = ""
    ) -> MemberCommandResult:
        """Run one member command for the active person and workspace.

        The command runs in this process, on a worker thread of its own that
        starts from an empty context: the broker's server task still carries
        whatever the turn that started it had bound. A command that outlasts
        the timeout is reported and left to finish on its own.

        A request the broker refuses (expired grant, wrong person, oversized
        input) comes back as an ``exit_code`` 2 result instead of a raised
        error: providers such as Antigravity fold MCP tool errors into the
        whole run's status, failing a turn the agent already recovered from.
        Only infrastructure failures escape as exceptions.
        """
        async with self._command_lock:
            context = self._context
            if context is None:
                return _rejected("No GuildBotics turn is active.")
            if not secrets.compare_digest(turn_grant, self._turn_grant):
                return _rejected("The GuildBotics turn grant is invalid or expired.")
            return await self.run(
                context.person_id,
                arguments,
                member_invocation(
                    context.conversation_key.work_kind,
                    context.run_id,
                    context.trace_id,
                    context.lease,
                    participant_labels=context.participant_labels,
                ),
                cwd=context.cwd,
                stdin=stdin,
            )

    async def run(
        self,
        person_id: str,
        arguments: list[str],
        invocation: MemberInvocation,
        *,
        cwd: Path,
        stdin: str,
    ) -> MemberCommandResult:
        """Run one member command as ``person_id`` with ``invocation``, handed
        the command's microVM (see :meth:`execute`).

        A turn's member command runs here once its grant is checked, and so
        does one its command asks for itself, under the command's grant.
        """
        if len(stdin.encode()) > _MAX_STDIN_BYTES:
            return _rejected("Member command stdin is too large.")
        if reason := _rejection_reason(arguments, person_id):
            return _rejected(reason)
        # Imported here: the member CLI is the layer above this one.
        from guildbotics.cli.member import run_in_process

        if self._guest is not None:
            invocation = replace(
                invocation,
                guest=self._guest.until(time.monotonic() + _COMMAND_TIMEOUT_SECONDS),
            )
        command = partial(run_in_process, arguments, invocation, cwd=cwd, stdin=stdin)
        work = asyncio.get_running_loop().run_in_executor(
            None, contextvars.Context().run, command
        )
        try:
            exit_code, stdout, stderr = await asyncio.wait_for(
                work, timeout=_COMMAND_TIMEOUT_SECONDS
            )
        except TimeoutError:
            return MemberCommandResult(
                exit_code=124,
                stdout="",
                stderr="Member capability command timed out; it may still complete.",
            )
        return MemberCommandResult(
            exit_code=exit_code, stdout=_bounded(stdout), stderr=_bounded(stderr)
        )

    async def call_host(self, request: Request) -> Response:
        """Answer one call of the command's environment.

        A call the command's grant refuses is a 403; one that fails, a 422;
        both with what :class:`HostCallError` carries. A call has the time a
        member command has; what it started on a worker thread may outlast it.
        """
        authorization = request.headers.get("authorization", "")
        if not secrets.compare_digest(authorization, f"Bearer {self._token}"):
            return Response(status_code=401)
        if _host_name(request.headers.get("host", "")) not in _ALLOWED_HOSTS:
            return Response(status_code=421)
        try:
            body = json.dumps({"result": await self._answer(request)})
        except HostCallError as exc:
            status = 403 if exc.category == "refused" else 422
            return JSONResponse({"error": exc.payload()}, status_code=status)
        if len(body.encode()) > _MAX_OUTPUT_BYTES:
            error = HostCallError("failed", "The call's result is too large.")
            return JSONResponse({"error": error.payload()}, status_code=422)
        return Response(body, media_type="application/json")

    async def _answer(self, request: Request) -> Any:
        """What the command's grant answers to ``request``.

        Raises:
            HostCallError: With what refused or failed the call.
        """
        if self._host is None:
            raise HostCallError("refused", "No GuildBotics command is running.")
        # Read up to the limit, however the body is sent: a chunked one
        # declares no length to check first.
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > _MAX_REQUEST_BYTES:
                raise HostCallError("refused", "The call is too large.")
        try:
            arguments = json.loads(body)
        except ValueError as exc:
            raise HostCallError("refused", "A call takes a JSON object.") from exc
        if not isinstance(arguments, dict):
            raise HostCallError("refused", "A call takes a JSON object.")
        work = asyncio.create_task(
            self._host(request.path_params["call"], arguments),
            context=self._host_context.copy(),
        )
        self._calls.add(work)
        work.add_done_callback(self._calls.discard)
        try:
            return await asyncio.wait_for(work, _COMMAND_TIMEOUT_SECONDS)
        except HostCallError:
            raise
        except AgentRuntimeError as exc:
            raise HostCallError(exc.category.value, str(exc), exc.details) from exc
        except TimeoutError as exc:
            raise HostCallError("failed", "The host call timed out.") from exc
        except Exception as exc:
            raise HostCallError("failed", str(exc) or type(exc).__name__) from exc

    async def settle(self) -> None:
        """Stop answering the command's calls, and wait for those being
        answered: each runs to its end, before what it uses is closed."""
        self._host = None
        await asyncio.gather(*self._calls, return_exceptions=True)

    async def close(self) -> None:
        """Revoke the token, stop answering the command's calls, and stop the
        loopback server."""
        self._context = None
        self._turn_grant = ""
        self._host = None
        server = self._server
        self._server = None
        self._url = ""
        self._port = 0
        self._token = secrets.token_urlsafe(32)
        if server is not None:
            await server.stop()

    async def _start(self) -> None:
        def app(port: int) -> Any:
            origin = f"http://{LOOPBACK_HOST}:{port}"
            with _root_logging_kept():
                mcp = MCPServer(
                    "GuildBotics Member",
                    instructions=(
                        "Use guildbotics_member for every command documented as "
                        "`guildbotics member ...`. Pass only the arguments after `member`."
                    ),
                    token_verifier=_ScopedTokenVerifier(self._token),
                    auth=AuthSettings(
                        issuer_url=AnyHttpUrl(origin),
                        resource_server_url=AnyHttpUrl(f"{origin}/mcp"),
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
                redirects, heredocs, `guildbotics`, or `member` itself. A request
                the broker refuses returns ``exit_code`` 2 with the reason in
                ``stderr``.
                """
                return await self.execute(turn_grant, arguments, stdin)

            # Not behind the MCP transport's bearer and Host checks: the
            # route makes its own.
            mcp.custom_route("/host/{call}", methods=["POST"])(self.call_host)

            return mcp.streamable_http_app(
                stateless_http=True,
                max_request_body_size=_MAX_REQUEST_BYTES,
                # The Host check keeps a rebinding page from reaching a loopback
                # server; a turn inside the agent environment names this host by
                # the gateway's alias, which is as much ours as loopback is.
                transport_security=TransportSecuritySettings(
                    allowed_hosts=[f"{host}:*" for host in _ALLOWED_HOSTS]
                ),
            )

        server = await LoopbackServer.start(app)
        self._server = server
        self._url = f"http://{LOOPBACK_HOST}:{server.port}/mcp"
        self._port = server.port


#: Members' brokers start on threads of their own, each putting back the root
#: logger it found: one at a time, or one finds another's MCPServer settings.
_ROOT_LOGGING = threading.Lock()


@contextmanager
def _root_logging_kept() -> Iterator[None]:
    """Put the process's root logger back as it was: MCPServer sets it (its
    level, and a handler of its own) as it is made, which is not its to set."""
    root = logging.getLogger()
    with _ROOT_LOGGING:
        level, handlers = root.level, root.handlers[:]
        try:
            yield
        finally:
            root.setLevel(level)
            root.handlers[:] = handlers


def _host_name(header: str) -> str:
    """The name a Host header gives, without its port."""
    name, _, port = header.rpartition(":")
    return name if name and port.isdigit() else header


def _as_guest(url: str) -> str:
    """``url`` as the agent environment reaches it."""
    return url.replace(LOOPBACK_HOST, GUEST_HOST_ALIAS, 1)


def member_invocation(
    work_kind: str,
    run_id: str,
    trace_id: str,
    lease: PersonExecutionLease | None,
    *,
    participant_labels: str = "",
) -> MemberInvocation:
    """What a member command asked for by ``run_id``'s work of ``work_kind``
    runs with: a chat run's, or a task run's for any other work."""
    chat = work_kind == "chat"
    return MemberInvocation(
        run_id=run_id if chat else "",
        task_run_id="" if chat else run_id,
        participant_labels=participant_labels,
        trace_id=trace_id,
        lease=lease,
    )


def _rejected(reason: str) -> MemberCommandResult:
    """Present one refused request as a command result the agent can read."""
    return MemberCommandResult(exit_code=2, stdout="", stderr=reason)


def _rejection_reason(arguments: list[str], person_id: str) -> str | None:
    """Explain why the broker refuses these arguments, or ``None`` to run."""
    if not arguments:
        return "Member command arguments must not be empty."
    if len(arguments) > _MAX_ARGUMENTS:
        return "Member command has too many arguments."
    if sum(len(value.encode()) for value in arguments) > _MAX_ARGUMENT_BYTES:
        return "Member command arguments are too large."
    if any("\0" in value for value in arguments):
        return "Member command arguments must not contain NUL bytes."
    if any(
        value == "--workspace" or value.startswith("--workspace=")
        for value in arguments
    ):
        return "The member capability workspace cannot be overridden."
    people: list[str] = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--person":
            if index + 1 >= len(arguments):
                return "--person requires the active member ID."
            people.append(arguments[index + 1])
            index += 2
            continue
        if value.startswith("--person="):
            people.append(value.partition("=")[2])
        index += 1
    if people and any(value != person_id for value in people):
        return "Member capabilities cannot act as another person."
    if not people and arguments != ["help"]:
        return "Member commands must name the active person with --person."
    return None


def _bounded(value: str) -> str:
    encoded = value.encode()
    if len(encoded) <= _MAX_OUTPUT_BYTES:
        return value
    return encoded[:_MAX_OUTPUT_BYTES].decode(errors="ignore") + "\n[output truncated]"
