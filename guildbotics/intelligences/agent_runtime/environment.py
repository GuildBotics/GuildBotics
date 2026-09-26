"""Where a provider process runs: inside the isolated agent environment.

Every adapter starts its provider CLI the same way, through
:func:`start_turn_environment`, in a microVM booted from this device's
snapshot. The turns of one command execution (:func:`command_environment`)
share one, and no turn runs outside a command. It boots before the first of
them and is shaped once for all, since a running microVM cannot be reshaped:
the turn's access contract, the persisted state of every AI CLI tool the
member is configured with, whatever of the workspace's own state the command
declares its turns inspect mounted read-only, and the ports of the member
broker and of each tool's credential gateway opened. It is discarded when the
command ends, however it ends. What changes from turn to turn is only what
can be given to a running microVM: the working directory, the environment,
and the login the turn is lent.

The adapter runs the CLI inside and speaks its protocol over the bridged
stdio. Nothing of the host -- its environment variables, its credentials,
its PATH -- reaches the provider, because the provider does not run on the
host.

What GuildBotics still runs on the host is its own: the member CLI the broker
spawns for the agent, under the process-tree policy below.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import CommandAccess, InspectionScope
from guildbotics.intelligences.agent_environment.auth_gateway import (
    CredentialGateway,
    CredentialUnavailableError,
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
from guildbotics.intelligences.agent_environment.spec import (
    GUEST_HOST_ALIAS,
    AgentEnvironmentSpec,
    EnvironmentMount,
    build_environment_spec,
    guest_path,
)
from guildbotics.intelligences.agent_environment.status import device_status
from guildbotics.intelligences.agent_runtime.member_broker import (
    MemberCapabilityBroker,
    MemberCapabilityBrokerError,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentAdapter,
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
)
from guildbotics.intelligences.agent_runtime.windows_job import (
    WindowsJob,
    creation_flags,
    register_process_job,
    terminate_process_job,
)
from guildbotics.intelligences.cli_agents import CliAgentInfo, cli_agent_info
from guildbotics.utils.fileio import (
    get_template_path,
    get_workspace_config_dir,
    get_workspace_local_path,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.processes import terminate_posix_process_group

_WINDOWS = os.name == "nt"

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


def inspected_directories(
    scopes: Iterable[InspectionScope], workspace_root: Path
) -> dict[str, Path]:
    """The host directories a turn inspecting ``scopes`` reads, by name.

    Each is mounted read-only at its own path, the way the guest spells it,
    so this one table decides both what the turn sees and what it is told to
    look in; a directory that does not exist yet (no run recorded) is in
    neither. The workspace's ``.guildbotics`` stays closed otherwise.

    Args:
        scopes: What the turn's command declares it inspects.
        workspace_root: The selected workspace.

    Returns:
        ``diagnostics`` for the recorded runs; ``config`` and ``templates``
        for the workspace configuration and the packaged defaults commands
        and settings fall back to.
    """
    directories: dict[InspectionScope, dict[str, Path]] = {
        "diagnostics": {
            "diagnostics": get_workspace_local_path(
                "run", workspace_root=workspace_root
            )
        },
        "config": {
            "config": get_workspace_config_dir(workspace_root),
            "templates": get_template_path(),
        },
    }
    return {
        name: path
        for scope in sorted(scopes)
        for name, path in directories[scope].items()
        if path.is_dir()
    }


#: The environment the AI CLI turns of the running command share.
_COMMAND: ContextVar[_SharedEnvironment | None] = ContextVar(
    "guildbotics_command_environment", default=None
)


@asynccontextmanager
async def command_environment(access: CommandAccess) -> AsyncIterator[None]:
    """Run the AI CLI turns of one command execution in one microVM.

    Nothing boots until a turn needs it, and what did is discarded when the
    command ends, cancellation included. A command run inside another one
    shares that one's: a command and its subcommands are one isolation, held
    to the access the outer command declared.

    Args:
        access: What the command declares of its turns' access.

    Raises:
        CommandError: If a command run inside another declares other access;
            it cannot be given the isolation it declared.
    """
    shared = _COMMAND.get()
    if shared is not None:
        if shared.access != access:
            raise CommandError(
                "A command cannot run inside a command that declares other access."
            )
        yield
        return
    shared = _SharedEnvironment(access)
    token = _COMMAND.set(shared)
    try:
        yield
    finally:
        _COMMAND.reset(token)
        await shared.close()


def running_command() -> _SharedEnvironment:
    """The environment of the command running now.

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


async def start_turn_environment(
    context: AgentExecutionContext,
    tool_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> TurnEnvironment:
    """Start one turn of ``tool_name`` in the running command's environment.

    The command's microVM is booted by its first turn. The turn holds it
    until it is closed, and the command's next turn waits until then: the
    member broker serves one turn at a time.

    Args:
        context: The turn: its working directory and access contract.
        tool_name: The catalog name of the AI CLI tool.
        env: What the provider process starts with beyond the tool's own
            state variables, its gateway, and the member broker's token.

    Raises:
        AgentRuntimeError: ``configuration`` when no command is running,
            when this device cannot run the environment or holds no snapshot
            for the declaration, or when the command's microVM was not
            started for this turn (another contract, a tool it does not hold,
            a working directory outside what it mounted); ``authentication``
            when the tool is not logged in here; ``process`` when the microVM
            or the broker does not start. Nothing is widened: a turn that
            cannot be confined does not run.
    """
    return await running_command().turn(context, tool_name, env or {})


class TurnEnvironment:
    """One turn's hold on its microVM.

    ``spec`` is the microVM as the turn sees it: its mounts and network, and
    the turn's own working directory and environment, which every process
    the turn runs starts with. ``broker`` is the member broker, bound to
    this turn until it is closed.
    """

    def __init__(
        self,
        environment: AgentEnvironment,
        spec: AgentEnvironmentSpec,
        broker: MemberCapabilityBroker,
        end: Callable[[], Awaitable[None]],
    ) -> None:
        self._environment = environment
        self.spec = spec
        self.broker = broker
        self._end: Callable[[], Awaitable[None]] | None = end

    async def run(
        self, command: str, *args: str, limit: int, tty: bool = False
    ) -> EnvironmentProcess:
        """Start ``command`` in the turn's working directory and environment."""
        return await self._environment.run(
            command, *args, limit=limit, tty=tty, cwd=self.spec.cwd, env=self.spec.env
        )

    async def write_file(self, path: str, data: bytes) -> None:
        await self._environment.write_file(path, data)

    async def read_file(self, path: str) -> bytes | None:
        return await self._environment.read_file(path)

    async def close(self) -> None:
        """End the turn; idempotent.

        The login it was lent and its broker grant are revoked, and a microVM
        of its own is discarded; a command's stays for the command's next
        turn. Ending the processes the turn started is the adapter's.
        """
        end, self._end = self._end, None
        if end is not None:
            await end()


class _SharedEnvironment:
    """A microVM, what it was started with, and the turns it runs in turn."""

    def __init__(self, access: CommandAccess) -> None:
        #: What the command declared; every turn of it is held to this.
        self.access = access
        self._broker = MemberCapabilityBroker()
        self._turn = asyncio.Lock()
        self._environment: AgentEnvironment | None = None
        #: The host directory the microVM was booted working in.
        self._cwd = Path()
        #: What shaped the microVM; a turn asking for another shape is refused.
        self._shape: tuple[object, ...] = ()
        #: What GuildBotics bound of its own: no turn works in there.
        self._binds: frozenset[EnvironmentMount] = frozenset()
        #: The gateway of each tool the microVM was started able to run.
        self._gateways: dict[str, CredentialGateway] = {}
        #: The relays running, each started by its tool's first turn.
        self._relays: dict[str, EnvironmentProcess] = {}
        #: The native adapters the command's turns speak through, by member and
        #: adapter; they end with the command, and no other command's touch them.
        self.adapters: dict[tuple[str, str], AgentAdapter] = {}
        self.adapters_lock = asyncio.Lock()

    async def turn(
        self,
        context: AgentExecutionContext,
        tool_name: str,
        env: Mapping[str, str],
    ) -> TurnEnvironment:
        """Start a turn, booting the microVM first when none runs yet."""
        await self._turn.acquire()
        gateway: CredentialGateway | None = None

        async def end() -> None:
            if gateway is not None:
                gateway.revoke()
            await self._broker.deactivate(context)
            self._turn.release()

        try:
            tool, where = _ready(tool_name)
            # The login is read before anything boots: a tool that is not
            # logged in here refuses its turn, and only its turn.
            lent = await _lend(tool, where)
            try:
                await self._broker.activate(context)
            except MemberCapabilityBrokerError as exc:
                raise AgentRuntimeError(
                    AgentRuntimeErrorCategory.PROCESS,
                    "Could not start the trusted member capability broker.",
                ) from exc
            environment = self._environment or await self._boot(context, tool, where)
            self._admit(context, tool)
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
                    **env,
                },
            )
            await self._hand_over(environment, tool, lent, gateway)
        except BaseException:
            await end()
            raise
        return TurnEnvironment(environment, spec, self._broker, end)

    async def _boot(
        self,
        context: AgentExecutionContext,
        tool: CliAgentInfo,
        where: LoginEnvironment,
    ) -> AgentEnvironment:
        """Boot the microVM able to run every tool the member is configured
        with, whatever of them the first turn runs: one member's slots can
        name different tools, and which one a later turn uses is decided
        while the command runs. A tool not logged in here is booted able to
        run all the same, and refused only when a turn of it comes.

        A boot that fails leaves nothing running: the gateways it started are
        stopped, so the command's next turn boots afresh instead of starting
        a second listener beside one no one can stop."""
        try:
            return await self._boot_able_to_run(context, tool, where)
        except BaseException:
            gateways, self._gateways = self._gateways, {}
            for gateway in gateways.values():
                await gateway.close()
            raise

    async def _boot_able_to_run(
        self,
        context: AgentExecutionContext,
        tool: CliAgentInfo,
        where: LoginEnvironment,
    ) -> AgentEnvironment:
        tools = [
            tool,
            *(
                other
                for name in sorted(context.tools - {tool.name})
                if (other := cli_agent_info(name)).provision.provisioned
            ),
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
                for mount in bind_state(each, read_only=context.contract.read_only)
            ),
            *(
                EnvironmentMount(guest_path(path), path, readonly=True)
                for path in inspected_directories(
                    context.inspects, context.workspace_root
                ).values()
            ),
        )
        spec = build_environment_spec(
            context.contract,
            context.cwd,
            host_ports=(
                self._broker.endpoint.port,
                *(gateway.port for gateway in self._gateways.values()),
            ),
            # A tool reaches its API through its gateway only: what it would
            # send straight to the provider carries the stand-in.
            provider_domains=[
                domain for each in tools for domain in each.provision.turn_domains
            ],
            nameservers=where.nameservers,
            mounts=binds,
        )
        self._environment = await _start(spec, where, before_stop=self._stop_relays)
        self._cwd = context.cwd
        self._shape = self._shape_of(context)
        self._binds = frozenset(binds)
        return self._environment

    def _admit(self, context: AgentExecutionContext, tool: CliAgentInfo) -> None:
        """Refuse a turn the running microVM was not started for.

        Its working directory must be inside what the microVM mounted of the
        turn's contract: the deepest mount it is under is one of the
        contract's, backed by the host or the microVM's own working directory.
        """
        assert self._environment is not None
        if self._shape_of(context) != self._shape or tool.name not in self._gateways:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.CONFIGURATION,
                t(
                    "intelligences.agent_environment.runtime.not_started_for",
                    tool=tool.label,
                ),
            )
        spec = self._environment.spec
        cwd = guest_path(context.cwd)
        mount = max(
            (
                mount
                for mount in spec.mounts
                if PurePosixPath(cwd).is_relative_to(mount.guest)
            ),
            key=lambda mount: len(PurePosixPath(mount.guest).parts),
            default=None,
        )
        if (
            mount is None
            or mount in self._binds
            or (mount.host is None and mount.guest != spec.cwd)
        ):
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.CONFIGURATION,
                t(
                    "intelligences.agent_environment.runtime.outside_mounts",
                    path=context.cwd,
                ),
            )

    def _shape_of(self, context: AgentExecutionContext) -> tuple[object, ...]:
        """What the turn's contract makes of the microVM: its mounts and its
        network, and what the turn inspects. Compared rather than the contract
        itself, so a change the microVM does not show -- a closed directory
        appearing outside everything mounted -- refuses no turn, while one it
        would show -- the same inside a mount -- does."""
        spec = build_environment_spec(context.contract, self._cwd, nameservers=())
        return spec.mounts, spec.network, context.inspects

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

    async def close(self) -> None:
        """Close the command's adapters, discard the microVM, then stop the
        gateways and the broker; the stand-ins open nothing once the microVM
        is gone. Idempotent."""
        adapters, self.adapters = list(self.adapters.values()), {}
        environment, self._environment = self._environment, None
        try:
            for adapter in adapters:
                await adapter.close()
        finally:
            try:
                if environment is not None:
                    await environment.close()
            finally:
                try:
                    for gateway in self._gateways.values():
                        await gateway.close()
                finally:
                    await self._broker.close()


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


def _ready(tool_name: str) -> tuple[CliAgentInfo, LoginEnvironment]:
    """Everything a start needs from this device, or the reason it cannot start.

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
    assert status.declaration is not None
    resources = status.declaration.resources
    return tool, LoginEnvironment(
        snapshot=status.snapshot.path,
        memory_mib=resources.memory_mib,
        cpus=resources.cpus,
        nameservers=status.dns.nameservers,
    )


async def _start(
    spec: AgentEnvironmentSpec,
    where: LoginEnvironment,
    *,
    before_stop: Callable[[AgentEnvironment], Awaitable[None]],
) -> AgentEnvironment:
    try:
        return await AgentEnvironment.start(
            spec,
            snapshot=str(where.snapshot),
            memory_mib=where.memory_mib,
            cpus=where.cpus,
            before_stop=before_stop,
        )
    except AgentEnvironmentError as exc:
        raise AgentRuntimeError(AgentRuntimeErrorCategory.PROCESS, str(exc)) from exc


async def terminate_process_tree(
    process: asyncio.subprocess.Process, *, grace_seconds: float = 2.0
) -> None:
    """Terminate the process group and reap the owned host subprocess."""
    pid = getattr(process, "pid", None)
    if _WINDOWS:
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=grace_seconds)
                return
            except TimeoutError:
                pass
        terminated = terminate_process_job(process)
        if not terminated and process.returncode is None:
            raise RuntimeError("Agent subprocess has no Windows Job Object.")
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(process.wait())
        return

    if process.returncode is not None:
        # The direct child may have exited while background descendants still
        # hold inherited pipes or continue working in its process group.
        if os.name == "posix" and pid:
            with suppress(ProcessLookupError):
                terminate_posix_process_group(pid)
        await process.wait()
        return
    if os.name == "posix" and pid:
        with suppress(ProcessLookupError):
            terminate_posix_process_group(pid)
    else:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass
    if os.name == "posix" and pid:
        with suppress(ProcessLookupError):
            terminate_posix_process_group(pid, force=True)
    else:
        with suppress(ProcessLookupError):
            process.kill()
    with suppress(asyncio.CancelledError, Exception):
        await asyncio.shield(process.wait())


async def create_agent_subprocess(
    *program: str,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    """Create a host subprocess of GuildBotics' own under the process-tree policy."""
    if not _WINDOWS:
        return await asyncio.create_subprocess_exec(*program, **kwargs)

    job = WindowsJob.create()
    process: asyncio.subprocess.Process | None = None
    existing_flags = int(kwargs.pop("creationflags", 0))
    try:
        process = await asyncio.create_subprocess_exec(
            *program,
            creationflags=existing_flags | creation_flags(),
            **kwargs,
        )
        job.assign_and_resume(process.pid)
    except Exception:
        if process is not None:
            with suppress(Exception):
                job.terminate()
            with suppress(Exception):
                process.kill()
            with suppress(Exception):
                await process.wait()
        job.close()
        raise
    register_process_job(process, job)
    return process
