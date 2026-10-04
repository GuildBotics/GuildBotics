"""Where a command runs: inside the isolated agent environment.

The host starts every command in a microVM booted from this device's
snapshot when the command starts (:func:`command_environment`), and runs
GuildBotics' own command execution machinery in it
(:meth:`_SharedEnvironment.execute`): the command, its subcommands and the
AI CLI turns among them all run there, and the microVM is discarded when the
command ends, however it ends. It is shaped once for all, from the command,
since a running microVM cannot be reshaped: the command's working directory
and access contract, the running member's clone opened read-write for a
command that may write, the persisted state of every AI CLI tool the member
is configured with, the running process's own GuildBotics code, the
workspace's configuration, and whatever else of the workspace's own state the
command declares its turns inspect mounted read-only, and the ports of the
member broker and of each tool's credential gateway opened. Nothing of the
host -- its environment variables, its credentials, its PATH -- reaches the
command, because the command does not run on the host.

What only the host holds the command asks for through its window to the host
(the member broker's ``/host/<call>``, answered by the command's grant): a
turn starting and ending (:meth:`_SharedEnvironment.turn`, which lends the
turn its login), the run record, the records, the external inference calls,
the member's own commands. What those commands run of what the command can
write -- git in the member's clones -- they run back in the command's
microVM (:class:`EnvironmentGuest`).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from pydantic import ValidationError

from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import CommandAccess, InspectionScope
from guildbotics.intelligences.agent_environment.auth_gateway import (
    CredentialGateway,
    CredentialUnavailableError,
)
from guildbotics.intelligences.agent_environment.contract import (
    AccessContract,
    AccessContractError,
    load_local_grants,
    load_shared_grants,
    resolve_access,
    validate_mount_source,
)
from guildbotics.intelligences.agent_environment.credential_vault import (
    CredentialVaultError,
    vault_problem,
)
from guildbotics.intelligences.agent_environment.provider_state import (
    LentLogin,
    LoginEnvironment,
    bind_state,
    login_command,
    start_login_environment,
)
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironment,
    AgentEnvironmentError,
    EnvironmentProcess,
)
from guildbotics.intelligences.agent_environment.snapshot import CODE_ROOT
from guildbotics.intelligences.agent_environment.spec import (
    GUEST_HOST_ALIAS,
    AgentEnvironmentSpec,
    EnvironmentMount,
    build_environment_spec,
    guest_path,
)
from guildbotics.intelligences.agent_environment.status import (
    DeviceStatus,
    device_status,
)
from guildbotics.intelligences.agent_environment.toolchain import (
    ToolchainError,
    load_toolchain,
)
from guildbotics.intelligences.agent_runtime.command_guest import EnvironmentGuest
from guildbotics.intelligences.agent_runtime.host_client import (
    CommandReply,
    CommandRequest,
    admits,
)
from guildbotics.intelligences.agent_runtime.member_broker import (
    MemberBrokerEndpoint,
    MemberCapabilityBroker,
    MemberCapabilityBrokerError,
    MemberCommandResult,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
)
from guildbotics.intelligences.cli_agents import CliAgentInfo, cli_agent_info
from guildbotics.runtime.member_invocation import MemberInvocation
from guildbotics.runtime.person_lease import (
    PersonExecutionLease,
    current_person_lease,
)
from guildbotics.utils.fileio import (
    PACKAGE_ROOT,
    get_template_path,
    get_workspace_config_dir,
    get_workspace_local_path,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.log_utils import get_logger
from guildbotics.utils.process_limits import STREAM_READ_LIMIT
from guildbotics.utils.safe_paths import inspect_host_path, normalize_host_path

#: What every provider process starts with, beside the tool's own state
#: variables: git must never wait for a terminal that is not there.
_PROVIDER_ENV = {"GIT_TERMINAL_PROMPT": "0"}
#: The CAs a turn whose gateway answers over TLS trusts: the system's, and
#: the gateway's own.
_SYSTEM_CAS = "/etc/ssl/certs/ca-certificates.crt"
_TURN_CAS = "/etc/guildbotics/ca-certificates.crt"
#: Where a relayed host resolves inside the turn, and the relay listens.
_RELAY_ADDRESS = "127.0.0.2"
#: A TCP relay onto the gateway: ``node -e _RELAY <host> <port>``.
_RELAY = (
    "const net=require('net');"
    "net.createServer(c=>{const u=net.connect(+process.argv[2],process.argv[1]);"
    "c.pipe(u);u.pipe(c);c.on('error',()=>u.destroy());u.on('error',()=>c.destroy())})"
    f".listen(443,'{_RELAY_ADDRESS}',()=>console.log('ready'))"
)
#: How long the relay has to start, and to stop.
_RELAY_SECONDS = 10.0
#: The command execution machinery's entry inside the microVM.
_ENTRY = "guildbotics.runtime.command_entry"
#: How much of the entry's output is read at a time, and the most kept of one
#: line it logs.
_CHUNK_BYTES = 1 << 16
_MAX_LOG_LINE_BYTES = 1 << 14
#: How long the entry's log is read after its reply: a process the command
#: left running holds it open, and ends with the microVM.
_LOG_DRAIN_SECONDS = 2.0
#: A line the entry logs: its level, then its message.
_LOG_LINE = re.compile(r"(DEBUG|INFO|WARNING|ERROR|CRITICAL) (.*)", re.DOTALL)
_PACKAGE = normalize_host_path(PACKAGE_ROOT)
CODE_MOUNT = EnvironmentMount(str(CODE_ROOT / "guildbotics"), _PACKAGE, readonly=True)


def code_path(path: Path) -> str:
    """Where a file of the running process's own package is inside a microVM."""
    return str(PurePosixPath(CODE_MOUNT.guest, path.relative_to(_PACKAGE).as_posix()))


def command_path(path: Path) -> str:
    """Where a command file is inside the command's microVM: a packaged one in
    the code every microVM mounts, a workspace one in its configuration."""
    return code_path(path) if path.is_relative_to(_PACKAGE) else guest_path(path)


def inspected_directories(
    scopes: Iterable[InspectionScope], workspace_root: Path
) -> dict[str, str]:
    """The directories a turn inspecting ``scopes`` reads, by name, as the
    guest spells them: what the turn is told to look in.

    The workspace's own state is mounted read-only at its own path
    (:func:`_inspected_mounts`), so the two cannot disagree; a directory that
    does not exist yet (no run recorded) is in neither. The workspace's
    ``.guildbotics`` stays closed otherwise. The packaged defaults are inside
    the code every microVM mounts.

    Args:
        scopes: What the turn's command declares it inspects.
        workspace_root: The selected workspace.

    Returns:
        ``diagnostics`` for the recorded runs; ``config`` and ``templates``
        for the workspace configuration and the packaged defaults commands
        and settings fall back to.
    """
    directories = {
        name: mount.guest
        for name, mount in _inspected_mounts(scopes, workspace_root).items()
    }
    if "config" in scopes:
        directories["templates"] = code_path(get_template_path())
    return directories


def _inspected_mounts(
    scopes: Iterable[InspectionScope], workspace_root: Path
) -> dict[str, EnvironmentMount]:
    """The workspace's own directories a turn inspecting ``scopes`` mounts."""
    directories: dict[InspectionScope, dict[str, Path]] = {
        "diagnostics": {
            "diagnostics": get_workspace_local_path(
                "run", workspace_root=workspace_root
            )
        },
        "config": {"config": get_workspace_config_dir(workspace_root)},
    }
    return {
        name: EnvironmentMount(guest_path(path), path, readonly=True)
        for scope in sorted(scopes)
        for name, path in directories[scope].items()
        if inspect_host_path(path, missing=True).present
    }


#: The environment of the running command.
_COMMAND: ContextVar[_SharedEnvironment | None] = ContextVar(
    "guildbotics_command_environment", default=None
)


class CommandGrant(Protocol):
    """What answers the calls of a command's microVM (a ``HostWindow``)."""

    async def __call__(self, name: str, arguments: dict[str, Any]) -> Any:
        """Answer the call ``name`` with ``arguments``."""
        ...

    def variables(
        self, endpoint: MemberBrokerEndpoint, mounts: Mapping[str, bool]
    ) -> dict[str, str]:
        """What the microVM is started with to reach the window and know the
        command."""
        ...


@asynccontextmanager
async def command_environment(
    access: CommandAccess,
    tools: frozenset[str],
    *,
    cwd: Path,
    workspace_root: Path,
    clone: Path,
    host: CommandGrant,
) -> AsyncIterator[_SharedEnvironment]:
    """Boot the microVM one command execution runs in, for as long as it runs.

    What shapes it is settled here, once for the whole command: the access
    contract, read from the workspace's settings, the tools its turns may
    run, and where it works. It is discarded when the command ends,
    cancellation included; the calls of it being answered are waited for,
    or, when the command did not end well, stopped.

    Args:
        access: What the command declares of its turns' access.
        tools: Every AI CLI tool the member is configured with; which one a
            turn uses is decided while the command runs.
        cwd: The command's working directory, which the microVM's is.
        workspace_root: The workspace the command runs in, whose
            configuration it reads and whose own state its turns may inspect.
        clone: The running member's clone, where a turn works on its tickets
            and chats; unless the command is read-only, made on the host
            before the microVM boots when absent, and mounted read-write.
        host: The command's grant, which answers what its microVM asks of
            the host, in the command's context.

    Raises:
        CommandError: If the settings the contract is read from are invalid
            or unreadable, or if this device cannot run the environment now,
            in the words the device's status gives.
        AgentRuntimeError: ``process`` when the microVM or the broker does
            not start.
    """
    shared = _SharedEnvironment(
        access,
        _contract(access),
        tools,
        cwd=cwd,
        workspace_root=workspace_root,
        clone=clone,
        where=_device(),
    )
    token = _COMMAND.set(shared)
    shared.serve(host)
    try:
        await shared.boot(host)
        yield shared
    except BaseException:
        _COMMAND.reset(token)
        await shared.close(abandon=True)
        raise
    _COMMAND.reset(token)
    await shared.close()


def _contract(access: CommandAccess) -> AccessContract:
    """What the command's turns may reach, as the workspace's settings say.

    Raises:
        CommandError: If the settings are invalid or cannot be read.
    """
    try:
        return AccessContract(
            network=load_toolchain().network,
            access=resolve_access(load_shared_grants(), load_local_grants()),
            read_only=access.read_only,
        )
    except (AccessContractError, ToolchainError) as exc:
        raise CommandError(str(exc)) from exc


def running_command() -> _SharedEnvironment:
    """The environment of the command running now, as the host answers its
    calls.

    Raises:
        AgentRuntimeError: ``configuration`` when no command is running.
    """
    shared = _COMMAND.get()
    if shared is None:
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.CONFIGURATION,
            "An AI CLI turn runs only inside a command.",
        )
    return shared


def current_command_access() -> CommandAccess:
    """The access the running command declared; none outside a command.

    A turn outside a command is refused when it starts, so what this reports
    there only has to be the narrowest thing that asks for nothing.
    """
    shared = _COMMAND.get()
    return shared.access if shared is not None else CommandAccess()


def command_lease() -> PersonExecutionLease | None:
    """The execution lease the running command's turns hold: none for a
    read-only command, which changes nothing and so runs while the member is
    busy."""
    return None if current_command_access().read_only else current_person_lease()


def current_command_contract() -> AccessContract | None:
    """The access contract of the command running now; none outside one.

    For what only records a turn and must not fail on its account.
    """
    shared = _COMMAND.get()
    return shared.contract if shared is not None else None


async def start_turn_environment(
    context: AgentExecutionContext, tool_name: str
) -> TurnEnvironment:
    """Start one turn of ``tool_name`` in the running command's microVM.

    The turn holds the microVM's turn until it is closed, and the command's
    next turn waits until then: the member broker serves one turn at a time.

    Args:
        context: The turn: its working directory and who it runs for.
        tool_name: The catalog name of the AI CLI tool.

    Raises:
        AgentRuntimeError: ``configuration`` when no command is running, or
            when the command's microVM was not started for this turn (a tool
            it does not hold, a working directory outside what it mounted);
            ``authentication`` when the tool is not logged in here;
            ``process`` when the broker cannot take the turn. Nothing is
            widened: a turn that cannot be confined does not run.
    """
    return await running_command().turn(context, tool_name)


class TurnEnvironment:
    """One turn's hold on the command's microVM.

    ``spec`` is the microVM as the turn sees it: its mounts and network, and
    the turn's own working directory and environment, which the provider the
    command starts for it starts with. ``broker`` is the member broker, bound
    to this turn until it is closed.
    """

    def __init__(
        self,
        spec: AgentEnvironmentSpec,
        broker: MemberCapabilityBroker,
        end: Callable[[], Awaitable[None]],
    ) -> None:
        self.spec = spec
        self.broker = broker
        self._end: Callable[[], Awaitable[None]] | None = end

    async def close(self) -> None:
        """End the turn; idempotent. The login it was lent and its broker
        grant are revoked; the microVM stays for the command's next turn."""
        end, self._end = self._end, None
        if end is not None:
            await end()


class _SharedEnvironment:
    """A command's microVM, what it was started with, and the turns it runs
    in turn."""

    def __init__(
        self,
        access: CommandAccess,
        contract: AccessContract,
        tools: frozenset[str],
        *,
        cwd: Path,
        workspace_root: Path,
        clone: Path,
        where: LoginEnvironment,
    ) -> None:
        #: What the command declared; every turn of it is held to this.
        self.access = access
        #: What the command may reach beyond its working directory. A
        #: read-only contract is what lets a command hold no execution lease
        #: and run while the member is busy: the environment, not the
        #: command or its provider, keeps it from changing anything.
        self.contract = contract
        #: The tools the microVM is booted able to run; a turn of another is
        #: refused.
        self.tools = tools
        #: Where the command works: the microVM's own working directory, the
        #: workspace whose configuration it reads and whose state its turns
        #: may inspect, and the member's clone its turns may work in.
        self._cwd = cwd
        self._workspace_root = workspace_root
        self._clone = clone
        #: What this device boots microVMs from.
        self._where = where
        self._environment: AgentEnvironment | None = None
        self._broker = MemberCapabilityBroker(
            EnvironmentGuest(asyncio.get_running_loop(), lambda: self._environment)
        )
        self._turn = asyncio.Lock()
        #: The microVM's mounts, by where it spells them, and whether work may
        #: happen under each.
        self._mounts: dict[str, bool] = {}
        #: The gateway of each tool the microVM was started able to run.
        self._gateways: dict[str, CredentialGateway] = {}
        #: The relays running, each started by its tool's first turn.
        self._relays: dict[str, EnvironmentProcess] = {}

    @property
    def endpoint(self) -> MemberBrokerEndpoint:
        """Where the microVM reaches the member broker and the command's
        window to the host."""
        return self._broker.endpoint

    async def turn(
        self, context: AgentExecutionContext, tool_name: str
    ) -> TurnEnvironment:
        """Start a turn of the running microVM."""
        await self._turn.acquire()
        gateway: CredentialGateway | None = None

        async def end() -> None:
            if gateway is not None:
                gateway.revoke()
            await self._broker.deactivate(context)
            self._turn.release()

        try:
            tool, where = _ready(tool_name)
            if tool.name not in self.tools:
                raise AgentRuntimeError(
                    AgentRuntimeErrorCategory.CONFIGURATION,
                    t(
                        "intelligences.agent_environment.runtime.not_started_for",
                        tool=tool.label,
                    ),
                )
            # The login is read first: a tool that is not logged in here
            # refuses its turn, and only its turn.
            lent = await _lend(tool, where)
            environment = self._environment
            assert environment is not None
            self._admit(context)
            try:
                await self._broker.activate(context)
            except MemberCapabilityBrokerError as exc:
                raise AgentRuntimeError(
                    AgentRuntimeErrorCategory.PROCESS,
                    "Could not start the trusted member capability broker.",
                ) from exc
            gateway = self._gateways[tool.name]
            gateway.lend(lent.access_token, lent.stand_in)
            context.login.refusal = lent.refusal
            broker = tool.provision.credential_broker
            assert broker is not None
            spec = replace(
                environment.spec,
                cwd=guest_path(context.cwd),
                env={
                    **environment.spec.env,
                    **_PROVIDER_ENV,
                    **tool.provision.environment(environment.spec.home),
                    **gateway.turn_environment(),
                    **({"SSL_CERT_FILE": _TURN_CAS} if broker.tls else {}),
                    **lent.stand_in_environment(),
                    **self._broker.provider_environment(),
                },
            )
            await self._hand_over(environment, tool, lent, gateway)
        except BaseException:
            await end()
            raise
        return TurnEnvironment(spec, self._broker, end)

    def serve(self, host: CommandGrant) -> None:
        """Answer what the microVM asks of the host with ``host``, in the
        context the command runs in now."""
        self._broker.serve(host, contextvars.copy_context())

    async def member(
        self,
        person_id: str,
        arguments: list[str],
        invocation: MemberInvocation,
        stdin: str,
    ) -> MemberCommandResult:
        """Run a member command the command asks for itself, where it works,
        as a turn's member commands run."""
        return await self._broker.run(
            person_id, arguments, invocation, cwd=self._cwd, stdin=stdin
        )

    async def boot(self, host: CommandGrant) -> None:
        """Boot the microVM, able to run every tool the member is configured
        with, whatever of them a turn runs: one member's slots can name
        different tools, and which one a turn uses is decided while the
        command runs. A tool not logged in here is booted able to run all the
        same, and refused only when a turn of it comes. The microVM is told
        what ``host`` says it must know.

        A boot that fails leaves nothing running: the gateways it started are
        stopped.

        Raises:
            CommandError: A host directory cannot be mounted into the
                environment.
            AgentRuntimeError: ``process`` when the microVM or the broker
                does not start.
        """
        try:
            try:
                await self._boot(host)
            except AccessContractError as exc:
                raise CommandError(str(exc)) from exc
        except BaseException:
            gateways, self._gateways = self._gateways, {}
            for gateway in gateways.values():
                await gateway.close()
            raise

    async def _boot(self, host: CommandGrant) -> None:
        try:
            await self._broker.start()
        except MemberCapabilityBrokerError as exc:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                "Could not start the trusted member capability broker.",
            ) from exc
        tools = [
            each
            for name in sorted(self.tools)
            if (each := cli_agent_info(name)).provision.provisioned
        ]
        for each in tools:
            broker = each.provision.credential_broker
            assert broker is not None
            gateway = self._gateways[each.name] = CredentialGateway(broker)
            await gateway.start()
        binds = (
            *(
                mount
                for each in tools
                for mount in bind_state(
                    each,
                    read_only=self.contract.read_only,
                    denied=self.contract.access.denied,
                )
            ),
            CODE_MOUNT,
            # The command reads the workspace's configuration, as it would on
            # the host; its turns are told of it only when they inspect it.
            *_inspected_mounts(
                {"config", *self.access.inspects}, self._workspace_root
            ).values(),
        )
        # What is mounted must exist before the microVM boots; a read-only
        # command mounts no clone and changes nothing on the host.
        if not self.contract.read_only:
            validate_mount_source(self._clone, self.contract.access.denied, create=True)
        spec = build_environment_spec(
            self.contract,
            self._cwd,
            worktrees=(self._clone,),
            host_ports=(
                self._broker.endpoint.port,
                *(gateway.port for gateway in self._gateways.values()),
            ),
            # A tool reaches its API through its gateway only: what it would
            # send straight to the provider carries the stand-in.
            provider_domains=[
                domain for each in tools for domain in each.provision.turn_domains
            ],
            nameservers=self._where.nameservers,
            mounts=binds,
        )
        _reject_windows_temp_mounts(spec)
        # Work happens only inside what the contract opened, backed by the
        # host or the microVM's own working directory; never in what
        # GuildBotics bound for itself, nor under a cover over a deny.
        self._mounts = {
            mount.guest: mount not in binds
            and (mount.host is not None or mount.guest == spec.cwd)
            for mount in spec.mounts
        }
        spec = replace(
            spec,
            env={
                **spec.env,
                **host.variables(self._broker.endpoint, self._mounts),
            },
        )
        self._environment = await _start(
            spec, self._where, before_stop=self._stop_relays
        )

    async def execute(self, request: CommandRequest) -> CommandReply:
        """Run the command in the microVM, and read how it ended.

        What it logs is logged on the host, line by line, as it comes.

        Raises:
            CommandError: When it ended without saying how, or said more than
                the host reads.
        """
        environment = self._environment
        assert environment is not None
        guest = EnvironmentGuest(asyncio.get_running_loop(), lambda: environment)
        argv = guest.python(_ENTRY)
        try:
            process = await environment.run(argv[0], *argv[1:], limit=_CHUNK_BYTES)
        except AgentEnvironmentError as exc:
            raise CommandError(str(exc)) from exc
        logging_task = asyncio.create_task(_relay_log(process.stderr))
        try:
            process.stdin.write(request.model_dump_json().encode())
            await process.stdin.drain()
            process.stdin.close()
            reply = await _read_reply(process.stdout)
            await asyncio.wait({logging_task}, timeout=_LOG_DRAIN_SECONDS)
        except BaseException:
            await process.kill()
            raise
        finally:
            logging_task.cancel()
        try:
            return CommandReply.model_validate_json(reply)
        except ValidationError as exc:
            raise CommandError(
                t(
                    "intelligences.agent_environment.runtime.no_reply",
                    code=await process.wait(),
                )
            ) from exc

    def _admit(self, context: AgentExecutionContext) -> None:
        """Refuse a turn working where the running microVM does not let work
        happen (:func:`admits`)."""
        if not admits(self._mounts, guest_path(context.cwd)):
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.CONFIGURATION,
                t(
                    "intelligences.agent_environment.runtime.outside_mounts",
                    path=context.cwd,
                ),
            )

    async def _hand_over(
        self,
        environment: AgentEnvironment,
        tool: CliAgentInfo,
        lent: LentLogin,
        gateway: CredentialGateway,
    ) -> None:
        """Give the running microVM what the turn is lent: the stand-in login
        files, the trust in its gateway's CA, and the relay to its gateway."""
        broker = tool.provision.credential_broker
        assert broker is not None
        root = f"{environment.spec.home}/{tool.provision.state_root}"
        try:
            for name, data in lent.stand_in_files().items():
                await environment.write_file(f"{root}/{name}", data)
            if broker.tls:
                await _trust(environment, gateway.ca_pem)
            if broker.relayed_hosts and tool.name not in self._relays:
                self._relays[tool.name] = await _relay(
                    environment, broker.relayed_hosts, gateway.port
                )
        except AgentEnvironmentError as exc:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS, str(exc)
            ) from exc

    async def _stop_relays(self, _: AgentEnvironment) -> None:
        # A relay that does not end goes with the microVM.
        for relay in self._relays.values():
            with suppress(TimeoutError):
                await asyncio.wait_for(relay.kill(), _RELAY_SECONDS)

    async def close(self, *, abandon: bool = False) -> None:
        """Stop answering the microVM's calls, once those being answered end
        -- or at once when ``abandon``: the command did not end well, and
        what it asked for is no longer wanted -- then discard the microVM, and
        stop the gateways and the broker; the stand-ins open nothing once the
        microVM is gone. Idempotent."""
        await self._broker.settle(abandon=abandon)
        environment, self._environment = self._environment, None
        try:
            if environment is not None:
                await environment.close()
        finally:
            try:
                for gateway in self._gateways.values():
                    await gateway.close()
            finally:
                await self._broker.close()


async def _read_reply(stdout: asyncio.StreamReader) -> bytes:
    """The entry's reply: the one line it writes last, or what it wrote before
    it ended without one; never more than the host reads.

    Raises:
        CommandError: When it is larger than that.
    """
    reply = bytearray()
    while not reply.endswith(b"\n") and (chunk := await stdout.read(_CHUNK_BYTES)):
        reply += chunk
        if len(reply) > STREAM_READ_LIMIT:
            raise CommandError(
                t("intelligences.agent_environment.runtime.reply_too_large")
            )
    return bytes(reply)


async def _relay_log(stderr: asyncio.StreamReader) -> None:
    """Log on the host what the entry logs, line by line at its own level; a
    line that names none (a traceback's) goes at the level of the one before."""
    logger = get_logger()
    level = logging.INFO
    pending = b""

    def log(line: bytes) -> None:
        nonlocal level
        text = line.decode(errors="replace").rstrip()
        if match := _LOG_LINE.fullmatch(text):
            level, text = logging.getLevelNamesMapping()[match[1]], match[2]
        if text:
            logger.log(level, text)

    while chunk := await stderr.read(_CHUNK_BYTES):
        *lines, pending = (pending + chunk).split(b"\n")
        for line in lines:
            log(line[:_MAX_LOG_LINE_BYTES])
        pending = pending[:_MAX_LOG_LINE_BYTES]
    if pending:
        log(pending)


async def _trust(environment: AgentEnvironment, ca_pem: bytes) -> None:
    """Make the turn trust ``ca_pem`` beside the system's CAs.

    Raises:
        AgentEnvironmentError: When the image has no system CAs to add it to;
            trusting it alone would fail every other TLS of the turn.
    """
    system = await environment.read_file(_SYSTEM_CAS)
    if not system:
        raise AgentEnvironmentError(
            t("intelligences.agent_environment.runtime.no_system_cas", path=_SYSTEM_CAS)
        )
    await environment.write_file(_TURN_CAS, system + b"\n" + ca_pem)


async def _relay(
    environment: AgentEnvironment, hosts: Iterable[str], port: int
) -> EnvironmentProcess:
    """Resolve ``hosts`` inside the turn to a relay onto the gateway's
    ``port``, which answers for them, and start it.

    Raises:
        AgentEnvironmentError: When the relay does not start.
    """
    known = await environment.read_file("/etc/hosts") or b""
    lines = "".join(f"{_RELAY_ADDRESS} {host}\n" for host in hosts)
    await environment.write_file("/etc/hosts", known + b"\n" + lines.encode())
    relay = await environment.run(
        "node", "-e", _RELAY, GUEST_HOST_ALIAS, str(port), limit=1 << 10
    )
    try:
        started = await asyncio.wait_for(relay.stdout.readline(), _RELAY_SECONDS)
    except TimeoutError:
        started = b""
    if started.strip() != b"ready":
        await relay.kill()
        raise AgentEnvironmentError(
            t("intelligences.agent_environment.runtime.relay_failed")
        )
    return relay


async def start_probe_environment(tool_name: str) -> AgentEnvironment:
    """Boot an environment for asking the tool about itself: its usage, its
    model catalog. It is asked where its login is: in an environment that
    holds it in memory and nothing else (see ``provider_state``).
    """
    tool, where = _ready(tool_name)
    try:
        return await start_login_environment(tool, where)
    except CredentialUnavailableError as exc:
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.AUTHENTICATION, str(exc)
        ) from exc
    except AgentEnvironmentError as exc:
        raise AgentRuntimeError(AgentRuntimeErrorCategory.PROCESS, str(exc)) from exc


async def _lend(tool: CliAgentInfo, where: LoginEnvironment) -> LentLogin:
    """The login a turn is lent, refreshed first when it is due.

    A tool reaches its API as soon as it starts, and may give up on it
    sooner than a refresh takes (Antigravity's sign-in waits ten seconds), so
    the refresh is not left to the first request.
    """
    try:
        lent = LentLogin(tool, where)
        await lent.access_token(None)
    except CredentialVaultError as exc:
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.AUTHENTICATION,
            vault_problem(exc.state, tool=tool.label, command=login_command(tool.name)),
        ) from exc
    except CredentialUnavailableError as exc:
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.AUTHENTICATION, str(exc)
        ) from exc
    return lent


def _device() -> LoginEnvironment:
    """What this device boots a command's microVM from.

    Raises:
        CommandError: If it cannot boot one now, in the device's own words
            (:mod:`..agent_environment.status`), the same ones the CLI and
            the Desktop show.
    """
    status = device_status()
    if status.snapshot is None or status.refusal:
        raise CommandError(status.refusal)
    return _login_environment(status)


def _ready(tool_name: str) -> tuple[CliAgentInfo, LoginEnvironment]:
    """Everything a turn of the tool needs from this device, or the reason it
    cannot start.

    The reasons are the device's own words (:mod:`..agent_environment.status`),
    the same ones the CLI and the Desktop show, so a refused turn and the
    status beside it never disagree.
    """
    tool = cli_agent_info(tool_name)
    status = device_status()
    if status.snapshot is None or status.refusal:
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.CONFIGURATION,
            status.refusal,
            details={"snapshot": status.snapshot.state} if status.snapshot else {},
        )
    tool_status = status.tool(tool_name)
    if tool_status.refusal:
        raise AgentRuntimeError(
            (
                AgentRuntimeErrorCategory.AUTHENTICATION
                if tool_status.provisioned
                else AgentRuntimeErrorCategory.CONFIGURATION
            ),
            tool_status.refusal,
        )
    return tool, _login_environment(status)


def _login_environment(status: DeviceStatus) -> LoginEnvironment:
    assert status.snapshot is not None and status.declaration is not None
    resources = status.declaration.resources
    return LoginEnvironment(
        snapshot=status.snapshot.path,
        memory_mib=resources.memory_mib,
        cpus=resources.cpus,
        nameservers=status.dns.nameservers,
    )


def _reject_windows_temp_mounts(spec: AgentEnvironmentSpec) -> None:
    """Reject Windows temporary directories that microsandbox cannot mount.

    See https://github.com/superradcompany/microsandbox/issues/1692.
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if sys.platform != "win32" or not local_app_data:
        return
    temporary = normalize_host_path(Path(local_app_data) / "Temp")
    temporary_facts = inspect_host_path(temporary, missing=True)
    for mount in spec.mounts:
        if mount.host is not None and temporary_facts.contains(
            inspect_host_path(mount.host, directory=False)
        ):
            raise CommandError(
                t(
                    "intelligences.agent_environment.runtime.windows_temp_mount",
                    path=mount.host,
                    temporary=temporary,
                )
            )


async def _start(
    spec: AgentEnvironmentSpec,
    where: LoginEnvironment,
    *,
    before_stop: Callable[[AgentEnvironment], Awaitable[None]],
) -> AgentEnvironment:
    try:
        return await where.start(spec, before_stop=before_stop)
    except AgentEnvironmentError as exc:
        raise AgentRuntimeError(AgentRuntimeErrorCategory.PROCESS, str(exc)) from exc
