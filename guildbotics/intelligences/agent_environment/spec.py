"""From the sandbox contract to what one turn's microVM is made of.

The contract names host directories and a network mode; the microVM needs
mounts, a network policy, a working directory, and an environment. This module
is the single translation, and it knows no provider and no runtime: an adapter
hands over the contract and gets back a :class:`AgentEnvironmentSpec` it cannot loosen.

Filesystem: every granted directory -- the working directory, the documents,
the device-local paths -- is mounted at the same path it has on the host, and
the guest's home directory is the host's home path, so a path means the same
thing on both sides: what the user typed, what the Desktop shows, and what the
provider's session state records all agree. The worktrees the command works
in beside its working directory (the running member's clone) are read-write;
a grant is read-only when it says so. The working directory itself opens
nothing for writing: a command works in it directly only under a read-write
grant, and anywhere else on a copy of it on the microVM's own disk, the host
directory mounted read-only (:class:`WorktreeCopy`). A read-only contract
makes every grant read-only, the working directory an empty directory of the
microVM's own, and mounts no worktree, so what the turn may not change holds
whatever provider runs it; only what
GuildBotics binds itself (``mounts``) keeps its own access. Nothing else of the host is
mounted except what GuildBotics binds itself (``mounts``), so credentials, the
workspace configuration, and other members' clones are unreachable rather than
forbidden -- unless a caller lets its turn inspect the workspace's own state. A
worktree is an explicit child of protected state, opened for its own sake.
Every host source is checked without following links; a source containing a
protected directory is refused, including read-only sources. No deny covers
are mounted. The trees a device's PATH derives are not mounted at all: the
agent's tools live inside the environment.

Network: the microVM's gateway enforces the one rule the contract states.
Unrestricted opens all egress; otherwise egress is closed except DNS, the
domains the contract allows, the provider's own API domains, and the host
ports GuildBotics itself needs. A read-only turn is allowed no domains of its
own, whatever the workspace declares.

Environment: nothing of the host's environment is inherited. What the guest
is told of the host is :func:`host_environment`, which every environment
starts with.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from zoneinfo import ZoneInfo

from tzlocal import get_localzone_name, reload_localzone

from guildbotics.intelligences.agent_environment.contract import (
    AccessContract,
    AccessContractError,
    DeniedPath,
    NetworkPolicy,
    ResolvedAccess,
    builtin_denied,
    validate_mount_source,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.os_language import os_ui_language
from guildbotics.utils.safe_paths import inspect_host_path, normalize_host_path

#: How the guest names the host: the address at which a host port the policy
#: opens (the member broker's) is reached from inside. The broker accepts it
#: as a Host header, and a turn is told the broker's URL with it.
GUEST_HOST_ALIAS = "host.microsandbox.internal"
#: Where a working directory the command works on a copy of is mounted,
#: read-only, for the copy to be made from (:class:`WorktreeCopy`).
WORKTREE_SOURCE = "/run/guildbotics/worktree"

_LOGGER = getLogger(__name__)


class AgentEnvironmentSpecError(ValueError):
    """Raised when the contract names something the environment cannot mount."""


@dataclass(frozen=True, slots=True)
class EnvironmentMount:
    """One mount inside the microVM.

    ``host`` is the host directory bound at ``guest``, or None for an empty
    directory of the microVM's own: a read-only turn's working directory or
    a login's state. It holds
    nothing of the host and is discarded with the microVM, so it is writable:
    a mount nested under it needs a mount point made there.
    """

    guest: str
    host: Path | None
    readonly: bool
    user: bool = False


@dataclass(frozen=True, slots=True)
class EnvironmentNetwork:
    """What the microVM's gateway lets out.

    ``unrestricted`` opens all egress; otherwise egress is closed except DNS,
    ``domains`` (exact names, or ``*.example.com`` for a suffix), the host's
    ``host_ports`` over TCP, and -- with ``local_network`` -- the host and its
    private networks entirely. Nothing is ever let in.
    """

    unrestricted: bool
    domains: tuple[str, ...]
    host_ports: tuple[int, ...]
    local_network: bool
    nameservers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorktreeCopy:
    """A working directory the command works on a copy of.

    No grant lets the command write there, so the host directory is mounted
    read-only at :data:`WORKTREE_SOURCE` and copied onto the microVM's own
    disk at the working directory's own path; what changed in the copy is
    written back by the host when the command ends. ``identity`` is the
    directory's own (device, inode) when it was copied, so what is written
    back never lands in another directory put at its path meanwhile.
    ``excluded`` are the directories under it, relative to it, that are
    mounts of their own (grants nested in it): neither copied nor written
    back.
    """

    host: Path
    identity: tuple[int, int]
    excluded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentEnvironmentSpec:
    """Everything the runtime needs to build one turn's microVM.

    ``home`` is the guest's home directory: the host's, as the guest spells
    it. The snapshot the turn boots from was built with the same home, so
    the provider's state is where the provider looks for it.
    """

    cwd: str
    home: str
    mounts: tuple[EnvironmentMount, ...]
    network: EnvironmentNetwork
    env: Mapping[str, str]
    denied: tuple[DeniedPath, ...] = ()
    worktree: WorktreeCopy | None = None

    @property
    def directories(self) -> dict[str, bool]:
        """The directories the microVM holds of its own, by where, and whether
        read-only: its mounts, and the copy of the working directory on its
        disk, which the command writes."""
        held = {mount.guest: mount.readonly for mount in self.mounts}
        if self.worktree is not None:
            held[guest_path(self.worktree.host)] = False
        return held


def build_environment_spec(
    contract: AccessContract,
    cwd: Path,
    *,
    worktrees: Iterable[Path] = (),
    host_ports: Iterable[int] = (),
    provider_domains: Iterable[str] = (),
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    nameservers: Iterable[str],
    mounts: Iterable[EnvironmentMount] = (),
) -> AgentEnvironmentSpec:
    """Translate the contract for a turn run in ``cwd``.

    Args:
        contract: What the turn may reach, as resolved on this device.
        cwd: The turn's working directory on the host.
        worktrees: Host directories the command works in beside ``cwd``,
            opened read-write like it: a turn may work in them. They are not
            grants, so a deny they are under does not close them; a
            read-only contract mounts none.
        host_ports: Host TCP ports the turn must always reach (the member
            broker), whatever the contract's network mode.
        provider_domains: The provider's own API domains, allowed whenever
            egress is restricted; they are GuildBotics' choice, not the user's.
        env: The environment the provider process starts with, beside
            :func:`host_environment`. The host's environment is never
            inherited: the environment is another machine.
        home: The host home directory, which is the guest's home too.
        nameservers: Upstream DNS resolvers for the environment's gateway,
            from the toolchain declaration. Without an explicit upstream,
            Codex's built-in resolver gets no answer from the gateway's
            default in time; naming one is what makes domain rules
            resolvable at all.
        mounts: What GuildBotics itself binds beyond the contract: the
            provider's persisted state, the cache turns share, an adapter's
            own scratch directory. They are the provider's business, not the
            user's grants, so the contract never lists them.
    """
    denied = tuple(
        dict.fromkeys(
            (
                *contract.access.denied,
                *builtin_denied(home),
            )
        )
    )
    cwd = normalize_host_path(cwd)
    access = ResolvedAccess(contract.access.documents, contract.access.paths, denied)
    opened, worktree = _mounts(
        access,
        cwd,
        () if contract.read_only else worktrees,
        read_only=contract.read_only,
    )
    all_mounts = (*opened, *mounts)
    # Every host bind, including provider files and code/config mounts, uses
    # the same path and protected-ancestor check. Guest tmpfs needs no host.
    for mount in all_mounts:
        if mount.host is not None:
            if normalize_host_path(mount.host) != mount.host:
                raise AgentEnvironmentSpecError(
                    f"Mount source must use its normalized OS spelling: {mount.host}"
                )
            validate_mount_source(mount.host, denied, grant=mount.user)
    return AgentEnvironmentSpec(
        cwd=guest_path(cwd),
        home=guest_home(home),
        mounts=all_mounts,
        network=_network(
            contract.reached_network,
            tuple(host_ports),
            tuple(provider_domains),
            nameservers,
        ),
        env={**host_environment(), **(env or {})},
        denied=denied,
        worktree=worktree,
    )


def host_environment() -> dict[str, str]:
    """What the guest is told of the host it stands in for, as variables.

    The microVM is another machine, but what runs in it works for the person
    at this one: a time it writes, a date it decides by, and the language it
    speaks must be theirs. Every environment starts with these, whatever runs
    in it, so they are facts of the host rather than settings of a provider.
    Each is read afresh, since the Desktop outlives a change of either, and a
    fact the host cannot name is left out rather than guessed.

    Returns:
        ``TZ``: the host's time zone by its IANA name, which the guest's
        zoneinfo reads (Windows' own names are mapped to it); without it the
        guest runs on UTC. ``LANGUAGE``: the host operating system's UI
        language as gettext names it (``ja_JP``), whatever the host keeps it
        in; gettext needs no locale installed in the guest to read it.
    """
    return {**_time_zone(), **_ui_language()}


def _time_zone() -> dict[str, str]:
    try:
        reload_localzone()
        zone = get_localzone_name()
        if zone:
            ZoneInfo(zone)
    except (LookupError, OSError, ValueError) as exc:
        _LOGGER.warning("The agent environment runs on UTC: %s", exc)
        return {}
    return {"TZ": zone} if zone else {}


def _ui_language() -> dict[str, str]:
    language = os_ui_language()
    if language is None:
        return {}
    return {"LANGUAGE": "_".join(filter(None, (language.language, language.territory)))}


def guest_home(home: Path | None = None) -> str:
    """The guest's home directory: the host's, as the guest spells it.

    The snapshot is built with this home and every turn runs with it, so
    both derive it here and cannot disagree.
    """
    return guest_path(normalize_host_path(home or Path.home()))


def guest_path(path: PurePath) -> str:
    """Where a host path appears inside the environment.

    A POSIX path keeps its spelling. A Windows drive becomes a top-level
    directory named after its letter (``C:\\work`` is ``/c/work``); the
    runtime's documentation leaves this to the caller, and this is the one
    convention GuildBotics uses.
    """
    if not path.is_absolute():
        raise AgentEnvironmentSpecError(f"'{path}' is not an absolute path")
    drive = path.drive
    if not drive:
        return path.as_posix()
    if not drive.endswith(":"):
        raise AgentEnvironmentSpecError(
            f"'{path}' is a network path and cannot be mounted"
        )
    return "/".join(("", drive[0].lower(), *path.parts[1:]))


def host_path(guest: str) -> Path:
    """The host path :func:`guest_path` spells as ``guest``.

    The guest is not trusted to spell it: only an absolute path written the
    way :func:`guest_path` writes one is read, never one that climbs out of
    where it seems to be.

    Raises:
        AgentEnvironmentSpecError: When ``guest`` is not such a path, or on
            Windows names no drive.
    """
    path = PurePosixPath(guest)
    if not path.is_absolute() or path.as_posix() != guest or ".." in path.parts:
        raise AgentEnvironmentSpecError(f"'{guest}' is not a normalized absolute path")
    if not isinstance(Path(), PureWindowsPath):
        return Path(path)
    drive, *rest = path.parts[1:] or ("",)
    if len(drive) != 1 or not drive.isalpha():
        raise AgentEnvironmentSpecError(f"'{guest}' names no drive")
    return Path(f"{drive.upper()}:\\", *rest)


def _mounts(
    access: ResolvedAccess, cwd: Path, worktrees: Iterable[Path], *, read_only: bool
) -> tuple[tuple[EnvironmentMount, ...], WorktreeCopy | None]:
    """The admitted host-backed mounts, outermost first, and the copy the
    command works on when it works on one.

    The working directory itself opens nothing for writing: only a grant
    does. A read-only turn has nothing of its own to read, so its working
    directory is an empty directory of the microVM's own. A command that may
    write works in its working directory directly only under a read-write
    grant; outside every grant it works on a copy, and the host directory --
    its ``.git`` with it -- is mounted read-only (:class:`WorktreeCopy`).

    Raises:
        AccessContractError: For a command that may write, run in a directory
            only a read-only grant opens.
    """
    cwd_guest = guest_path(cwd)
    granted = [g for g in (*access.documents, *access.paths) if g.present]
    for grant in granted:
        validate_mount_source(grant.path, access.denied, grant=True)
    target = inspect_host_path(cwd, missing=True)
    # The innermost grant holding the working directory decides, as its
    # mount is the one over it.
    covering = max(
        (g for g in granted if inspect_host_path(g.path).contains(target)),
        key=lambda g: len(g.path.parts),
        default=None,
    )
    copied = not read_only and covering is None
    if not read_only and covering is not None and covering.access == "read":
        raise AccessContractError(
            t("intelligences.agent_environment.runtime.read_only_cwd", path=cwd)
        )
    opened: dict[str, EnvironmentMount] = {
        cwd_guest: (
            EnvironmentMount(WORKTREE_SOURCE, cwd, readonly=True, user=True)
            if copied
            else EnvironmentMount(
                cwd_guest, None if read_only else cwd, readonly=False, user=True
            )
        )
    }
    for worktree in worktrees:
        opened.setdefault(
            guest_path(worktree),
            EnvironmentMount(guest_path(worktree), worktree, readonly=False),
        )
    for grant in granted:
        opened.setdefault(
            guest_path(grant.path),
            EnvironmentMount(
                guest_path(grant.path),
                grant.path,
                readonly=read_only or grant.access == "read",
                user=True,
            ),
        )
    mounts = sorted(
        opened.values(), key=lambda m: (len(PurePosixPath(m.guest).parts), m.guest)
    )
    if not copied:
        return tuple(mounts), None
    excluded = tuple(
        PurePosixPath(m.guest).relative_to(cwd_guest).as_posix()
        for m in mounts
        if PurePosixPath(m.guest).is_relative_to(cwd_guest)
    )
    return tuple(mounts), WorktreeCopy(cwd, target.identities[-1], excluded)


def _network(
    policy: NetworkPolicy,
    host_ports: tuple[int, ...],
    provider_domains: tuple[str, ...],
    nameservers: Iterable[str],
) -> EnvironmentNetwork:
    unrestricted = policy.mode == "unrestricted"
    domains = () if unrestricted else (*provider_domains, *policy.allowed_domains)
    return EnvironmentNetwork(
        unrestricted=unrestricted,
        domains=tuple(dict.fromkeys(domains)),
        host_ports=tuple(dict.fromkeys(host_ports)),
        local_network=policy.allow_local_network,
        nameservers=tuple(nameservers),
    )
