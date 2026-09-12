"""The toolchain declaration: what every agent environment of a workspace holds.

``config/intelligences/agent_environment.yml`` is shared between the devices
of a workspace, so every device builds the same environment from it: the
packages an agent finds inside, beyond the base image and the provider CLIs
GuildBotics itself puts there, and the upstream resolvers the environment's
DNS gateway forwards to. It is the whole of what a user declares. The base
image and the way packages are installed belong to the build recipe
(:mod:`.snapshot`), not to the declaration: they are how GuildBotics builds,
not what the user wants inside.
"""

from __future__ import annotations

import ipaddress
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
