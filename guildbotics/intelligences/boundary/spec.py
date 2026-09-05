"""From the sandbox contract to what one turn's microVM is made of.

The contract names host directories and network modes; the microVM needs
mounts, a network policy, a working directory, and an environment. This module
is the single translation, and it knows no provider and no runtime: an adapter
hands over the contract and gets back a :class:`BoundarySpec` it cannot loosen.

Filesystem: the working directory is mounted read-write at the same guest path,
a document directory at the same path below the guest home, a device-local
path at its own path, each read-only when the grant says so. Nothing else of
the host is mounted, so credentials, the workspace configuration, and other
members' clones are unreachable rather than forbidden. A deny inside an opened
tree is covered with an empty read-only mount; a deny outside one closes nothing
that was open. The trees a device's PATH derives are not mounted at all: the
agent's tools live inside the boundary.

Network: the microVM's gateway enforces the policy, so the provider's built-in
web tools and the commands it runs are one flow with one rule. The two routes
of the contract therefore combine: unrestricted on either opens everything,
otherwise egress is closed except DNS, the domains either route allows, the
provider's own API domains, and the host ports GuildBotics itself needs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath

from guildbotics.intelligences.sandbox import (
    NetworkPolicy,
    ResolvedAccess,
    ResolvedGrant,
    SandboxContract,
)

#: The home directory inside the boundary. Provider state and the snapshot's
#: toolchain live there, and document grants are mounted below it at the
#: relative path the grant names.
GUEST_HOME = "/root"
#: Upstream resolvers the boundary's DNS gateway forwards to. Without an
#: explicit upstream, Codex's built-in resolver gets no answer from the
#: gateway's default in time and its login fails; naming one is what makes
#: domain rules resolvable at all.
DEFAULT_NAMESERVERS: tuple[str, ...] = ("1.1.1.1", "1.0.0.1")


class BoundarySpecError(ValueError):
    """Raised when the contract names something the boundary cannot mount."""


@dataclass(frozen=True, slots=True)
class BoundaryMount:
    """One mount inside the microVM.

    ``host`` is the host directory bound at ``guest``, or None for an empty
    mount that covers a denied corner of an opened tree.
    """

    guest: str
    host: Path | None
    readonly: bool


@dataclass(frozen=True, slots=True)
class BoundaryNetwork:
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
class BoundarySpec:
    """Everything the runtime needs to build one turn's microVM."""

    cwd: str
    mounts: tuple[BoundaryMount, ...]
    network: BoundaryNetwork
    env: Mapping[str, str]


def build_boundary_spec(
    contract: SandboxContract,
    cwd: Path,
    *,
    host_ports: Iterable[int] = (),
    provider_domains: Iterable[str] = (),
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    nameservers: Iterable[str] = DEFAULT_NAMESERVERS,
) -> BoundarySpec:
    """Translate the contract for a turn run in ``cwd``.

    Args:
        contract: What the turn may reach, as resolved on this device.
        cwd: The turn's working directory on the host.
        host_ports: Host TCP ports the turn must always reach (the member
            broker), whatever the contract's network mode.
        provider_domains: The provider's own API domains, allowed whenever
            egress is restricted; they are GuildBotics' choice, not the user's.
        env: The environment the provider process starts with. The host's
            environment is never inherited: the boundary is another machine.
        home: The host home directory document grants were resolved under.
        nameservers: Upstream DNS resolvers for the boundary's gateway.
    """
    home_root = (home or Path.home()).resolve()
    return BoundarySpec(
        cwd=guest_path(cwd),
        mounts=_mounts(contract.access, cwd, home_root),
        network=_network(
            contract.network, tuple(host_ports), tuple(provider_domains), nameservers
        ),
        env=dict(env or {}),
    )


def guest_path(path: PurePath) -> str:
    """Where a host path appears inside the boundary.

    A POSIX path keeps its spelling, so the provider's session state records
    the same working directory inside and outside. A Windows drive becomes a
    top-level directory named after its letter (``C:\\work`` is ``/c/work``);
    the runtime's documentation leaves this to the caller, and this is the
    one convention GuildBotics uses.
    """
    if not path.is_absolute():
        raise BoundarySpecError(f"'{path}' is not an absolute path")
    drive = path.drive
    if not drive:
        return path.as_posix()
    if not drive.endswith(":"):
        raise BoundarySpecError(f"'{path}' is a network path and cannot be mounted")
    return "/".join(("", drive[0].lower(), *path.parts[1:]))


def _mounts(
    access: ResolvedAccess, cwd: Path, home_root: Path
) -> tuple[BoundaryMount, ...]:
    """The host-backed mounts, outermost first, then the denies they cover."""
    opened: dict[str, BoundaryMount] = {}

    def open_(guest: str, host: Path, readonly: bool) -> None:
        opened.setdefault(guest, BoundaryMount(guest, host, readonly))

    open_(guest_path(cwd), cwd, readonly=False)
    for grant in _open_grants(access, access.documents):
        relative = grant.path.relative_to(home_root).as_posix()
        open_(f"{GUEST_HOME}/{relative}", grant.path, grant.access == "read")
    for grant in _open_grants(access, access.paths):
        open_(guest_path(grant.path), grant.path, grant.access == "read")
    covers = {
        f"{mount.guest}/{denied.path.relative_to(mount.host).as_posix()}"
        for denied in access.denied
        if denied.path.is_dir()
        for mount in opened.values()
        if mount.host is not None
        and denied.path != mount.host
        and denied.path.is_relative_to(mount.host)
    }
    mounts = [*opened.values(), *(BoundaryMount(g, None, True) for g in covers)]
    return tuple(
        sorted(mounts, key=lambda m: (len(PurePosixPath(m.guest).parts), m.guest))
    )


def _open_grants(
    access: ResolvedAccess, grants: Iterable[ResolvedGrant]
) -> Iterable[ResolvedGrant]:
    """The grants that stand: present on this device and not denied outright."""
    for grant in grants:
        if grant.present and not any(
            grant.path.is_relative_to(denied.path) for denied in access.denied
        ):
            yield grant


def _network(
    policy: NetworkPolicy,
    host_ports: tuple[int, ...],
    provider_domains: tuple[str, ...],
    nameservers: Iterable[str],
) -> BoundaryNetwork:
    routes = (policy.command, policy.web)
    unrestricted = any(route.mode == "unrestricted" for route in routes)
    domains = (
        ()
        if unrestricted
        else (*provider_domains, *(d for r in routes for d in r.allowed_domains))
    )
    return BoundaryNetwork(
        unrestricted=unrestricted,
        domains=tuple(dict.fromkeys(domains)),
        host_ports=tuple(dict.fromkeys(host_ports)),
        local_network=policy.command.allow_local_network,
        nameservers=tuple(nameservers),
    )
