"""The toolchain declaration: what every agent environment of a workspace holds.

``config/intelligences/agent_environment.yml`` is shared between the devices
of a workspace, so every device builds the same environment from it: the
base image the build starts from (GuildBotics' own unless the workspace
names one it built itself), the packages an agent finds inside beyond that
image and the provider CLIs GuildBotics itself puts there, and the upstream
resolvers the environment's DNS gateway forwards to. It is the whole of what
a user declares. The way packages are installed belongs to the build recipe
(:mod:`.snapshot`), not to the declaration: it is how GuildBotics builds,
not what the user wants inside.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from guildbotics.utils.fileio import get_config_path, load_yaml_file
from guildbotics.utils.i18n_tool import t

#: The shared declaration, resolved like every configuration file: the
#: workspace's own copy first, the package template otherwise.
TOOLCHAIN_PATH = "intelligences/agent_environment.yml"


class ToolchainError(ValueError):
    """Raised when the declaration is malformed."""


def _package_specs(specs: list[str]) -> list[str]:
    """A package spec is one argument to the package manager: never an option."""
    for spec in specs:
        if not spec or any(ch.isspace() for ch in spec) or spec.startswith("-"):
            raise ValueError(
                t(
                    "intelligences.agent_environment.declaration.not_a_package",
                    spec=spec,
                )
            )
    return specs


#: An image identity as the runtime reports it: the digest of the image's
#: configuration, which survives ``docker save`` / ``msb image load`` (a
#: manifest digest does not, since an archive re-encodes the manifest).
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
#: A CPU architecture as OCI names it (``amd64``, ``arm64``).
_ARCHITECTURE = re.compile(r"^[a-z0-9]+$")


class BaseImage(BaseModel):
    """The image the build starts from, when the workspace names one.

    ``reference`` is how the image is called on a device (``local/agent:1``)
    and ``digests`` says which image that is, per CPU architecture: the
    environment runs the device's own architecture, so an arm64 device and
    an amd64 device build the same Dockerfile into two images, and the
    declaration carries the identity of each. A reference is re-tagged
    whenever the image is rebuilt, so a device that holds the reference at
    another digest, or has no digest declared for its architecture, is told
    to load or build the declared image. The image itself is not shared;
    each device loads it (``guildbotics environment image load``) from an
    archive built for its architecture.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    reference: str
    digests: dict[str, str]

    @field_validator("reference")
    @classmethod
    def _one_reference(cls, reference: str) -> str:
        if (
            not reference
            or any(ch.isspace() for ch in reference)
            or reference.startswith("-")
        ):
            raise ValueError(
                t(
                    "intelligences.agent_environment.declaration.not_an_image",
                    reference=reference,
                )
            )
        return reference

    @field_validator("digests")
    @classmethod
    def _config_digests(cls, digests: dict[str, str]) -> dict[str, str]:
        if not digests:
            raise ValueError(
                t("intelligences.agent_environment.declaration.no_digests")
            )
        for architecture, digest in digests.items():
            if not _ARCHITECTURE.match(architecture):
                raise ValueError(
                    t(
                        "intelligences.agent_environment.declaration.not_an_architecture",
                        architecture=architecture,
                    )
                )
            if not _IMAGE_DIGEST.match(digest):
                raise ValueError(
                    t(
                        "intelligences.agent_environment.declaration.not_a_digest",
                        digest=digest,
                    )
                )
        return digests

    def digest_for(self, architecture: str) -> str:
        """The declared identity for ``architecture``, or "" when none is."""
        return self.digests.get(architecture, "")


class Packages(BaseModel):
    """Packages installed on top of the base image, by package manager.

    Each entry is passed to its manager as one argument, so a version is
    pinned the way that manager spells it (``ripgrep=14.1.0-1``,
    ``typescript@5.6.3``, ``ruff==0.6.9``). An unpinned entry drifts with
    every rebuild and the environment's name cannot tell.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    apt: list[str] = Field(default_factory=list)
    npm: list[str] = Field(default_factory=list)
    uv: list[str] = Field(default_factory=list)

    _specs = field_validator("apt", "npm", "uv")(_package_specs)


#: The declaration's word for "the resolvers this device uses".
HOST_NAMESERVERS = "host"
_RESOLV_CONF = Path("/etc/resolv.conf")


class DnsSettings(BaseModel):
    """Where the environment's DNS gateway forwards queries.

    The default is a list of public resolvers: the gateway drops answers
    that carry private addresses, so a device's own resolver adds nothing
    the environment can use, while its quirks (a home router that sends
    oversized UDP answers the gateway cannot relay) would break tools that
    treat SERVFAIL as failure. ``host`` names the IPv4 resolvers of
    whichever device runs the turn, read when it starts, for networks that
    block outside DNS; a shared list of one network's private resolvers
    would strand a device on another.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    nameservers: Literal["host"] | list[str]

    @field_validator("nameservers")
    @classmethod
    def _ipv4_addresses(cls, nameservers: str | list[str]) -> str | list[str]:
        if isinstance(nameservers, str):
            return nameservers
        if not nameservers:
            raise ValueError(
                t(
                    "intelligences.agent_environment.declaration.no_nameservers",
                    host=HOST_NAMESERVERS,
                )
            )
        for nameserver in nameservers:
            try:
                ipaddress.IPv4Address(nameserver)
            except ValueError as exc:
                raise ValueError(
                    t(
                        "intelligences.agent_environment.declaration.not_ipv4",
                        nameserver=nameserver,
                    )
                ) from exc
        return nameservers


def upstream_nameservers(dns: DnsSettings) -> tuple[str, ...]:
    """The resolvers an environment started now forwards to.

    Raises:
        ToolchainError: When the declaration says ``host`` and this device
            has no IPv4 resolver to read; the gateway speaks IPv4 only.
    """
    if dns.nameservers != HOST_NAMESERVERS:
        return tuple(dns.nameservers)
    resolvers = device_nameservers()
    if not resolvers:
        raise ToolchainError(
            t(
                "intelligences.agent_environment.declaration.no_device_resolver",
                where=TOOLCHAIN_PATH,
                host=HOST_NAMESERVERS,
            )
        )
    return resolvers


def device_nameservers() -> tuple[str, ...]:
    """This device's IPv4 resolvers, in the order it consults them."""
    if sys.platform == "win32":
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-DnsClientServerAddress -AddressFamily IPv4).ServerAddresses",
            ],
            capture_output=True,
            text=True,
            # The addresses are ASCII; anything else PowerShell prints (an
            # error in the console code page) must not break the read.
            errors="replace",
            check=False,
        )
        candidates = completed.stdout.split()
    else:
        try:
            lines = _RESOLV_CONF.read_text().splitlines()
        except OSError:
            lines = []
        candidates = [
            line.split()[1]
            for line in lines
            if line.startswith("nameserver") and len(line.split()) > 1
        ]
    resolvers: list[str] = []
    for candidate in candidates:
        try:
            ipaddress.IPv4Address(candidate)
        except ValueError:
            continue
        if candidate not in resolvers:
            resolvers.append(candidate)
    return tuple(resolvers)


class ToolchainDeclaration(BaseModel):
    """The shared declaration file as a whole."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Absent, the build starts from the image the recipe pins.
    image: BaseImage | None = None
    packages: Packages = Field(default_factory=Packages)
    dns: DnsSettings


def parse_toolchain(raw: Any, *, where: str) -> ToolchainDeclaration:
    """Validate a loaded declaration; ``where`` names it in the error."""
    if not isinstance(raw, dict):
        raise ToolchainError(
            t("intelligences.agent_environment.declaration.not_a_mapping", where=where)
        )
    try:
        return ToolchainDeclaration.model_validate(raw)
    except ValidationError as exc:
        raise ToolchainError(f"{where}: {exc}") from exc


def load_toolchain() -> ToolchainDeclaration:
    """Read the workspace's declaration, or the package template without one."""
    return parse_toolchain(
        load_yaml_file(get_config_path(TOOLCHAIN_PATH)), where=TOOLCHAIN_PATH
    )
