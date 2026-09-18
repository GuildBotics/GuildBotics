"""What a provider keeps between turns on this device, and logging in to it.

The environment is discarded after every turn, so a provider's login and its
conversation sessions live outside it, in a store this device keeps per
provider (``~/.guildbotics/data/agent_environment/<provider>/``) and every
member shares, exactly as they share the provider's state on the host today.
Only the entries the provider's provision names are ever kept from a turn:
the credentials and the sessions. The rest of the provider's directory --
its settings, skills, plugins, and everything else it reads its instructions
and its tools from -- is the snapshot's, so what an agent changes there is
gone with the turn.

Which of the two shapes a provider gets is the provision's to say. Most bind
their persisted entries from the store, and a turn writes into the store as
it goes. A provider that renames files into the state root itself cannot be
given bound files there, so it gets a directory of its own for the turn,
filled from the store with the persisted entries and nothing else; when the
turn ends, only those entries go back, and the directory is discarded with
whatever else the turn left in it.

Logging in is the one interactive step: the provider's own login command
runs inside an environment that mounts nothing but the provider's store and
has all egress open, because the addresses an OAuth exchange visits are the
provider's business and change with its versions. There is nothing of the
user's in that environment to carry anywhere.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
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
from guildbotics.utils.fileio import (
    atomic_write_bytes,
    atomic_write_text,
    get_machine_state_path,
)

#: This device's per-provider stores, and the cache turns keep between them.
STATE_ROOT = ("agent_environment",)
CACHE_DIR = "cache"
#: Where a turn's own copy of a writable state root lives: beside the store,
#: never inside it, so nothing reaches the store by being written there.
TURNS_DIR = "turns"
#: A turn directory older than this was left behind by a run that was killed:
#: no turn lasts a day, and the copy back only ever happens at its end.
_STALE_TURN_SECONDS = 24 * 60 * 60
#: Longest line the login command may print.
_LOGIN_LINE_LIMIT = 1 << 16


def provider_state_dir(tool: CliAgentInfo) -> Path:
    """The provider's store on this device: its state root, as it is under home."""
    return get_machine_state_path(*STATE_ROOT, tool.name, tool.provision.state_root)


def cache_dir() -> Path:
    """The ``~/.cache`` turns share, so what one turn fetched the next has."""
    return get_machine_state_path(*STATE_ROOT, CACHE_DIR)


def has_credentials(tool: CliAgentInfo) -> bool:
    """Whether the provider's credentials exist in this device's store."""
    provision = tool.provision
    return (
        bool(provision.auth) and (provider_state_dir(tool) / provision.auth).is_file()
    )


def authentication_failed(tool: CliAgentInfo) -> bool:
    """Whether the last known authentication outcome on this device failed."""
    return _failure_path(tool).is_file()


def record_authentication_outcome(tool: CliAgentInfo, *, failed: bool) -> None:
    """Keep only the latest outcome, shared by all members on this device.

    This marker is outside the provider's mounted store and contains no
    credentials or provider output. Unknown outcomes must not call this.
    """
    path = _failure_path(tool)
    if failed:
        atomic_write_text(path, "authentication failed\n")
    else:
        path.unlink(missing_ok=True)


def _failure_path(tool: CliAgentInfo) -> Path:
    return get_machine_state_path(*STATE_ROOT, tool.name, "authentication-failed")


@dataclass(frozen=True, slots=True)
class ProviderState:
    """One turn's hold on the provider's state, and what it gives back.

    ``release`` ends the hold: a turn that wrote into the store directly has
    nothing to do, and a turn that had a directory of its own gives back the
    persisted entries and loses the rest of it. It is called when the turn's
    environment is gone, once, whether the turn succeeded or not.
    """

    mounts: tuple[EnvironmentMount, ...]
    tool: CliAgentInfo
    turn_dir: Path | None = None

    def release(self) -> None:
        """Keep the persisted entries of a turn directory and discard it."""
        if self.turn_dir is None:
            return
        try:
            _copy_persisted(self.tool, self.turn_dir, provider_state_dir(self.tool))
        finally:
            shutil.rmtree(self.turn_dir, ignore_errors=True)


def bind_state(tool: CliAgentInfo, home: Path | None = None) -> ProviderState:
    """What a turn of ``tool`` binds of this device's store, and how it ends.

    A provider whose state root may be bound entry by entry gets its
    persisted entries from the store: directories are created there so a
    first turn can fill them, and a file is bound only once it exists,
    because a provider that finds an empty credentials file does not read it
    as being logged out. A provider that renames files into the root itself
    (``writable_root``) gets a directory of its own instead, holding a copy
    of the persisted entries and nothing else of the store.
    """
    provision = tool.provision
    root = f"{guest_home(home)}/{provision.state_root}"
    if provision.writable_root:
        turn_dir = _turn_dir(tool)
        _copy_persisted(tool, provider_state_dir(tool), turn_dir)
        mounts = [EnvironmentMount(root, turn_dir, False)]
    else:
        turn_dir = None
        store = provider_state_dir(tool)
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
    return ProviderState(tuple(mounts), tool, turn_dir)


def _turn_dir(tool: CliAgentInfo) -> Path:
    """An empty directory of this turn's own, beside the provider's store.

    What a killed run left behind is removed here, because a turn directory
    is only ever read by the turn that owns it and the copy back happens at
    that turn's end.
    """
    turns = get_machine_state_path(*STATE_ROOT, tool.name, TURNS_DIR)
    turns.mkdir(parents=True, exist_ok=True, mode=0o700)
    stale = time.time() - _STALE_TURN_SECONDS
    for left in turns.iterdir():
        with suppress(OSError):  # A turn starting beside this one may win it.
            if left.is_dir() and left.stat().st_mtime < stale:
                shutil.rmtree(left, ignore_errors=True)
    return Path(tempfile.mkdtemp(dir=turns))


def _copy_persisted(tool: CliAgentInfo, source_root: Path, target_root: Path) -> None:
    """Copy the persisted entries of one state root into another, only those.

    This is both halves of a writable root: the store into the turn's
    directory, where what a login or an earlier boundary left beside them
    stays behind and reaches no turn, and the turn's directory back into the
    store, where a session directory is merged rather than replacing what a
    turn running beside this one saved. A file is replaced whole, because a
    half-written credentials file is what the next turn would read as its
    login.
    """
    for entry in tool.provision.persisted:
        name = entry.rstrip("/")
        source, target = source_root / name, target_root / name
        if entry.endswith("/"):
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write_bytes(target, source.read_bytes())


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
    write: Callable[[str], None],
    home: Path | None = None,
) -> int:
    """Run the provider's login command inside the environment, interactively.

    The command gets a terminal: a tool that must ask before it stores its
    credentials in a file (Copilot, where there is no keychain) only asks on
    one.

    Args:
        tool: The provider to log in to; it must be provisioned.
        declaration: Where the environment's DNS gateway forwards to.
        snapshot: The snapshot to boot from.
        read_line: Blocks for one line the user typed, or None at the end
            of their input; it is called from a worker thread.
        write: Receives what the login command prints, as it comes; a
            prompt waits without a newline, so this is not line by line.
        home: The host home directory, the guest's too.

    Returns:
        The login command's exit code.
    """
    environment = await AgentEnvironment.start(
        login_spec(tool, declaration, home),
        snapshot=str(snapshot),
        memory_mib=declaration.resources.memory_mib,
        cpus=declaration.resources.cpus,
    )
    try:
        process = await environment.run(
            *tool.provision.login, limit=_LOGIN_LINE_LIMIT, tty=True
        )

        async def pump(reader: asyncio.StreamReader) -> None:
            while chunk := await reader.read(_LOGIN_LINE_LIMIT):
                write(chunk.decode(errors="replace"))

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
            code = await process.wait()
            if code == 0 and has_credentials(tool):
                record_authentication_outcome(tool, failed=False)
            return code
        finally:
            feeding.cancel()
    finally:
        await environment.close()
