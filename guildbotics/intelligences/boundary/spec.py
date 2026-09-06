"""From the sandbox contract to what one turn's microVM is made of.

The contract names host directories and a network mode; the microVM needs
mounts, a network policy, a working directory, and an environment. This module
is the single translation, and it knows no provider and no runtime: an adapter
hands over the contract and gets back a :class:`BoundarySpec` it cannot loosen.

Filesystem: every granted directory -- the working directory, the documents,
the device-local paths -- is mounted at the same path it has on the host, and
the guest's home directory is the host's home path, so a path means the same
thing on both sides: what the user typed, what the Desktop shows, and what the
provider's session state records all agree. The working directory is always
read-write; a grant is read-only when it says so. Nothing else of the host is
mounted, so credentials, the workspace configuration, and other members'
clones are unreachable rather than forbidden. A deny inside an opened tree is
covered with an empty read-only mount; a deny outside one closes nothing that
was open. The trees a device's PATH derives are not mounted at all: the
agent's tools live inside the boundary.

Network: the microVM's gateway enforces the one rule the contract states.
Unrestricted opens all egress; otherwise egress is closed except DNS, the
domains the contract allows, the provider's own API domains, and the host
ports GuildBotics itself needs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath

from guildbotics.intelligences.sandbox import (
    NetworkPolicy,
    ResolvedAccess,
    SandboxContract,
)

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
    """Everything the runtime needs to build one turn's microVM.

    ``home`` is the guest's home directory: the host's, as the guest spells
    it. The snapshot the turn boots from was built with the same home, so
    the provider's state is where the provider looks for it.
    """

    cwd: str
    home: str
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
        home: The host home directory, which is the guest's home too.
        nameservers: Upstream DNS resolvers for the boundary's gateway.
    """
    return BoundarySpec(
        cwd=guest_path(cwd),
        home=guest_path((home or Path.home()).resolve()),
        mounts=_mounts(contract.access, cwd),
        network=_network(
            contract.network, tuple(host_ports), tuple(provider_domains), nameservers
        ),
        env=dict(env or {}),
    )


def guest_path(path: PurePath) -> str:
    """Where a host path appears inside the boundary.

    A POSIX path keeps its spelling. A Windows drive becomes a top-level
    directory named after its letter (``C:\\work`` is ``/c/work``); the
    runtime's documentation leaves this to the caller, and this is the one
    convention GuildBotics uses.
    """
    if not path.is_absolute():
        raise BoundarySpecError(f"'{path}' is not an absolute path")
    drive = path.drive
    if not drive:
        return path.as_posix()
    if not drive.endswith(":"):
        raise BoundarySpecError(f"'{path}' is a network path and cannot be mounted")
    return "/".join(("", drive[0].lower(), *path.parts[1:]))


def _mounts(access: ResolvedAccess, cwd: Path) -> tuple[BoundaryMount, ...]:
    """The host-backed mounts, outermost first, then the denies they cover."""
    opened: dict[str, BoundaryMount] = {
        guest_path(cwd): BoundaryMount(guest_path(cwd), cwd, readonly=False)
    }
    for grant in (*access.documents, *access.paths):
        denied = any(grant.path.is_relative_to(d.path) for d in access.denied)
        if grant.present and not denied:
            opened.setdefault(
                guest_path(grant.path),
                BoundaryMount(
                    guest_path(grant.path), grant.path, grant.access == "read"
                ),
            )
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


def _network(
    policy: NetworkPolicy,
    host_ports: tuple[int, ...],
    provider_domains: tuple[str, ...],
    nameservers: Iterable[str],
) -> BoundaryNetwork:
    unrestricted = policy.mode == "unrestricted"
    domains = () if unrestricted else (*provider_domains, *policy.allowed_domains)
    return BoundaryNetwork(
        unrestricted=unrestricted,
        domains=tuple(dict.fromkeys(domains)),
        host_ports=tuple(dict.fromkeys(host_ports)),
        local_network=policy.allow_local_network,
        nameservers=tuple(nameservers),
    )
