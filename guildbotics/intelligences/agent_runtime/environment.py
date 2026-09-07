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
from collections.abc import Iterable, Mapping
from contextlib import suppress
from typing import Any

from guildbotics.capabilities.task_runs import RUN_ENV, TASK_RUN_ENV
from guildbotics.intelligences.agent_environment.provider_state import (
    is_logged_in,
    state_mounts,
)
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironment,
    AgentEnvironmentError,
    doctor,
)
from guildbotics.intelligences.agent_environment.snapshot import (
    SnapshotStatus,
    snapshot_status,
)
from guildbotics.intelligences.agent_environment.spec import (
    AgentEnvironmentSpec,
    EnvironmentMount,
    EnvironmentNetwork,
    build_environment_spec,
    guest_home,
)
from guildbotics.intelligences.agent_environment.toolchain import (
    ToolchainError,
    load_toolchain,
    upstream_nameservers,
)
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
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.processes import terminate_posix_process_group

CHAT_PARTICIPANT_LABELS_ENV = "GUILDBOTICS_CHAT_PARTICIPANT_LABELS"
_WINDOWS = os.name == "nt"

# asyncio's default 64 KiB StreamReader limit aborts readline() on single-line
# JSON payloads such as replayed tool results or aggregated command output.
STREAM_READ_LIMIT = 10 * 1024 * 1024

#: What every provider process starts with, beside the tool's own state
#: variables: git must never wait for a terminal that is not there.
_PROVIDER_ENV = {"GIT_TERMINAL_PROMPT": "0"}


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
    tool, status, nameservers = _ready(tool_name)
    home = guest_home()
    spec = build_environment_spec(
        context.contract,
        context.cwd,
        host_ports=host_ports,
        provider_domains=tool.provision.api_domains,
        env={**_PROVIDER_ENV, **tool.provision.environment(home), **env},
        nameservers=nameservers,
        mounts=(*state_mounts(tool), *mounts),
    )
    return await _start(spec, status)


async def start_probe_environment(tool_name: str) -> AgentEnvironment:
    """Boot an environment for asking the tool about itself: its usage, its
    model catalog. Only the tool's state is bound, and only its API is open."""
    tool, status, nameservers = _ready(tool_name)
    home = guest_home()
    spec = AgentEnvironmentSpec(
        cwd=home,
        home=home,
        mounts=state_mounts(tool),
        network=EnvironmentNetwork(
            unrestricted=False,
            domains=tool.provision.api_domains,
            host_ports=(),
            local_network=False,
            nameservers=nameservers,
        ),
        env={**_PROVIDER_ENV, **tool.provision.environment(home)},
    )
    return await _start(spec, status)


def _ready(tool_name: str) -> tuple[CliAgentInfo, SnapshotStatus, tuple[str, ...]]:
    """Everything a start needs from this device, or the reason it cannot start."""
    tool = cli_agent_info(tool_name)
    health = doctor()
    if not health.available:
        raise AgentRuntimeError(AgentRuntimeErrorCategory.CONFIGURATION, health.reason)
    if not tool.provision.package:
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.CONFIGURATION,
            f"{tool.label} is not provisioned in the agent environment yet.",
        )
    try:
        declaration = load_toolchain()
        nameservers = upstream_nameservers(declaration.dns)
    except ToolchainError as exc:
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.CONFIGURATION, str(exc)
        ) from exc
    status = snapshot_status(declaration)
    if status.state != "ready":
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.CONFIGURATION,
            _not_ready(status),
            details={"snapshot": status.state},
        )
    if not is_logged_in(tool):
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.AUTHENTICATION,
            f"{tool.label} is not logged in on this device; run "
            f"`guildbotics environment login {tool.name}`.",
        )
    return tool, status, nameservers


def _not_ready(status: SnapshotStatus) -> str:
    if status.state == "building":
        return "The agent environment is being built on this device; try again when it is ready."
    if status.state == "failed":
        return f"The agent environment failed to build on this device: {status.detail}"
    return (
        f"The agent environment is {status.state} on this device; build it with "
        "`guildbotics environment build`."
    )


async def _start(
    spec: AgentEnvironmentSpec, status: SnapshotStatus
) -> AgentEnvironment:
    try:
        return await AgentEnvironment.start(spec, snapshot=str(status.path))
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
