"""What a provider keeps between turns on this device, and logging in to it.

The environment is discarded after every turn, so a provider's login and its
conversation sessions live outside it, in a store this device keeps per
provider (``~/.guildbotics/data/agent_environment/<provider>/``) and every
member shares, exactly as they share the provider's state on the host today.
Only the entries the provider's provision names are ever bound into a turn:
the credentials and the sessions. The rest of the provider's directory --
its settings, skills, plugins -- is the snapshot's, so what an agent changes
there is gone with the turn.

Logging in is the one interactive step: the provider's own login command
runs inside an environment that mounts nothing but the provider's store and
has all egress open, because the addresses an OAuth exchange visits are the
provider's business and change with its versions. There is nothing of the
user's in that environment to carry anywhere.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from guildbotics.intelligences.agent_environment.runtime import AgentEnvironment
from guildbotics.intelligences.agent_environment.spec import (
    AgentEnvironmentSpec,
    EnvironmentMount,
    EnvironmentNetwork,
    guest_home,
)
from guildbotics.intelligences.agent_environment.toolchain import (
    ToolchainDeclaration,
    upstream_nameservers,
)
from guildbotics.intelligences.cli_agents import CliAgentInfo
from guildbotics.utils.fileio import get_machine_state_path

#: This device's per-provider stores, and the cache turns keep between them.
STATE_ROOT = ("agent_environment",)
CACHE_DIR = "cache"
#: Longest line the login command may print.
_LOGIN_LINE_LIMIT = 1 << 16


def provider_state_dir(tool: CliAgentInfo) -> Path:
    """The provider's store on this device: its state root, as it is under home."""
    return get_machine_state_path(*STATE_ROOT, tool.name, tool.provision.state_root)


def cache_dir() -> Path:
    """The ``~/.cache`` turns share, so what one turn fetched the next has."""
    return get_machine_state_path(*STATE_ROOT, CACHE_DIR)


def is_logged_in(tool: CliAgentInfo) -> bool:
    """Whether the provider's credentials exist in this device's store."""
    provision = tool.provision
    return (
        bool(provision.auth) and (provider_state_dir(tool) / provision.auth).is_file()
    )


def state_mounts(
    tool: CliAgentInfo, home: Path | None = None
) -> tuple[EnvironmentMount, ...]:
    """The store entries a turn binds at their place under the guest's home.

    Directories are created in the store so a first turn can fill them; a
    file is bound only once it exists, because a provider that finds an
    empty credentials file does not read it as being logged out.
    """
    provision = tool.provision
    store = provider_state_dir(tool)
    root = f"{guest_home(home)}/{provision.state_root}"
    mounts = []
    for entry in provision.persisted:
        host = store / entry.rstrip("/")
        if entry.endswith("/"):
            host.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif not host.is_file():
            continue
        mounts.append(EnvironmentMount(f"{root}/{entry.rstrip('/')}", host, False))
    cache = cache_dir()
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    mounts.append(EnvironmentMount(f"{guest_home(home)}/.cache", cache, False))
    return tuple(mounts)


def login_spec(
    tool: CliAgentInfo, declaration: ToolchainDeclaration, home: Path | None = None
) -> AgentEnvironmentSpec:
    """The environment the provider's login command runs in.

    The whole store is bound at the provider's state root, so whatever the
    login writes -- the credentials first of all -- lands in the store.
    """
    store = provider_state_dir(tool)
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    guest = guest_home(home)
    return AgentEnvironmentSpec(
        cwd=guest,
        home=guest,
        mounts=(
            EnvironmentMount(f"{guest}/{tool.provision.state_root}", store, False),
        ),
        network=EnvironmentNetwork(
            unrestricted=True,
            domains=(),
            host_ports=(),
            local_network=False,
            nameservers=upstream_nameservers(declaration.dns),
        ),
        env=tool.provision.environment(guest),
    )


async def login(
    tool: CliAgentInfo,
    declaration: ToolchainDeclaration,
    *,
    snapshot: Path,
    read_line: Callable[[], str | None],
    write_line: Callable[[str], None],
    home: Path | None = None,
) -> int:
    """Run the provider's login command inside the environment, interactively.

    Args:
        tool: The provider to log in to; it must be provisioned.
        declaration: Where the environment's DNS gateway forwards to.
        snapshot: The snapshot to boot from.
        read_line: Blocks for one line the user typed, or None at the end
            of their input; it is called from a worker thread.
        write_line: Receives every line the login command prints.
        home: The host home directory, the guest's too.

    Returns:
        The login command's exit code.
    """
    environment = await AgentEnvironment.start(
        login_spec(tool, declaration, home), snapshot=str(snapshot)
    )
    try:
        process = await environment.run(*tool.provision.login, limit=_LOGIN_LINE_LIMIT)

        async def pump(reader: asyncio.StreamReader) -> None:
            while line := await reader.readline():
                write_line(line.decode(errors="replace").rstrip("\r\n"))

        async def feed() -> None:
            while (line := await asyncio.to_thread(read_line)) is not None:
                if process.returncode is not None:
                    return
                process.stdin.write(line.encode())
                await process.stdin.drain()
            process.stdin.close()

        feeding = asyncio.create_task(feed())
        try:
            await asyncio.gather(pump(process.stdout), pump(process.stderr))
            return await process.wait()
        finally:
            feeding.cancel()
    finally:
        await environment.close()
