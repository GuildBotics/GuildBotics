"""Where a provider process runs: inside the isolated agent environment.

Every adapter starts its provider CLI the same way, through
:func:`start_turn_environment`: a microVM booted from this device's snapshot
for the one turn, shaped by the turn's access contract, with the provider's
persisted state bound in and the member broker's port opened. The adapter
runs the CLI inside it and speaks its protocol over the bridged stdio; when
the turn ends the microVM is discarded. Nothing of the host -- its
environment variables, its credentials, its PATH -- reaches the provider,
because the provider does not run on the host.

What GuildBotics still runs on the host is its own: the member CLI the broker
spawns for the agent, under the process-tree policy below.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from typing import Any

from guildbotics.capabilities.task_runs import RUN_ENV, TASK_RUN_ENV
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
    guest_home,
)
from guildbotics.intelligences.agent_environment.status import device_status
from guildbotics.intelligences.agent_runtime.models import (
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
from guildbotics.observability import TRACE_ID_ENV
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.processes import terminate_posix_process_group

CHAT_PARTICIPANT_LABELS_ENV = "GUILDBOTICS_CHAT_PARTICIPANT_LABELS"
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


async def start_turn_environment(
    context: AgentExecutionContext,
    tool_name: str,
    *,
    host_ports: Iterable[int],
    env: Mapping[str, str],
    mounts: Iterable[EnvironmentMount] = (),
) -> AgentEnvironment:
    """Boot the environment one turn of ``tool_name`` runs in.

    Args:
        context: The turn: its working directory and access contract.
        tool_name: The catalog name of the AI CLI tool.
        host_ports: Host ports the turn must reach (the member broker's).
        env: What the provider process starts with beyond the tool's own
            state variables (the broker's bearer token).
        mounts: An adapter's own binds beyond the contract and the tool's
            persisted state.

    Raises:
        AgentRuntimeError: ``configuration`` when this device cannot run the
            environment or holds no snapshot for the declaration;
            ``authentication`` when the tool is not logged in here;
            ``process`` when the microVM does not start. Nothing is widened:
            a turn that cannot be confined does not run.
    """
    tool, where = _ready(tool_name)
    home = guest_home()
    broker = tool.provision.credential_broker
    assert broker is not None
    lent = _lend(tool, where)
    gateway = CredentialGateway(broker, lent.access_token, lent.stand_in)
    await gateway.start()
    try:
        spec = build_environment_spec(
            context.contract,
            context.cwd,
            host_ports=(*host_ports, gateway.port),
            # The tool reaches its API through the gateway only: what it would
            # send straight to the provider carries the stand-in.
            provider_domains=tool.provision.turn_domains,
            env={
                **_PROVIDER_ENV,
                **tool.provision.environment(home),
                **gateway.turn_environment(),
                **({"SSL_CERT_FILE": _TURN_CAS} if broker.tls else {}),
                **lent.stand_in_environment(),
                **env,
            },
            nameservers=where.nameservers,
            # A turn that evaluates input holds nothing of the store: no
            # session, no account, no cache.
            mounts=(*(() if context.input_only else bind_state(tool)), *mounts),
        )
    except BaseException:
        await gateway.close()
        raise
    relays: list[EnvironmentProcess] = []

    async def close_gateway(_: AgentEnvironment) -> None:
        # The stand-in opens nothing from before the microVM is gone; a relay
        # that does not end goes with the microVM.
        for relay in relays:
            with suppress(TimeoutError):
                await asyncio.wait_for(relay.kill(), _RELAY_SECONDS)
        await gateway.close()

    try:
        environment = await _start(spec, where, before_stop=close_gateway)
    except BaseException:
        await gateway.close()
        raise
    root = f"{home}/{tool.provision.state_root}"
    try:
        for name, data in lent.stand_in_files().items():
            await environment.write_file(f"{root}/{name}", data)
        if broker.tls:
            await _trust(environment, gateway.ca_pem)
        if broker.relayed_hosts:
            relays.append(await _relay(environment, broker.relayed_hosts, gateway.port))
    except BaseException as exc:
        await environment.close()
        if isinstance(exc, AgentEnvironmentError):
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS, str(exc)
            ) from exc
        raise
    return environment


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


def _lend(tool: CliAgentInfo, where: LoginEnvironment) -> LentLogin:
    try:
        return LentLogin(tool, where)
    except CredentialVaultError as exc:
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.AUTHENTICATION,
            vault_problem(exc.state, tool=tool.label, command=login_command(tool.name)),
        ) from exc


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


def member_command_environment(context: AgentExecutionContext) -> dict[str, str]:
    """Build verified-execution metadata for the host-side member CLI."""
    run_key = RUN_ENV if context.conversation_key.work_kind == "chat" else TASK_RUN_ENV
    env = {
        GUILDBOTICS_WORKSPACE_ROOT: str(context.workspace_data_root),
        run_key: context.run_id,
    }
    if context.participant_labels:
        env[CHAT_PARTICIPANT_LABELS_ENV] = context.participant_labels
    if context.trace_id:
        env[TRACE_ID_ENV] = context.trace_id
    return env


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
