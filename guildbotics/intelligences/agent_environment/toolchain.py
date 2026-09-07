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
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from guildbotics.utils.fileio import get_config_path, load_yaml_file

#: The shared declaration, resolved like every configuration file: the
#: workspace's own copy first, the package template otherwise.
TOOLCHAIN_PATH = "intelligences/agent_environment.yml"


class ToolchainError(ValueError):
    """Raised when the declaration is malformed."""


def _package_specs(specs: list[str]) -> list[str]:
    """A package spec is one argument to the package manager: never an option."""
    for spec in specs:
        if not spec or any(ch.isspace() for ch in spec) or spec.startswith("-"):
            raise ValueError(f"'{spec}' is not a package specification")
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


class DnsSettings(BaseModel):
    """Where the environment's DNS gateway forwards queries.

    The gateway's own default upstream does not answer Codex's built-in
    resolver in time, so the upstreams are always named. They are a property
    of the environment, shared like the rest of it: a device on another
    network must be able to reach them too.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    nameservers: list[str] = Field(min_length=1)

    @field_validator("nameservers")
    @classmethod
    def _ipv4_addresses(cls, nameservers: list[str]) -> list[str]:
        for nameserver in nameservers:
            try:
                ipaddress.IPv4Address(nameserver)
            except ValueError as exc:
                raise ValueError(f"'{nameserver}' is not an IPv4 address") from exc
        return nameservers


class ToolchainDeclaration(BaseModel):
    """The shared declaration file as a whole."""

    model_config = ConfigDict(extra="forbid", strict=True)

    packages: Packages = Field(default_factory=Packages)
    dns: DnsSettings


def parse_toolchain(raw: Any, *, where: str) -> ToolchainDeclaration:
    """Validate a loaded declaration; ``where`` names it in the error."""
    if not isinstance(raw, dict):
        raise ToolchainError(f"{where}: the declaration must be a mapping")
    try:
        return ToolchainDeclaration.model_validate(raw)
    except ValidationError as exc:
        raise ToolchainError(f"{where}: {exc}") from exc


def load_toolchain() -> ToolchainDeclaration:
    """Read the workspace's declaration, or the package template without one."""
    return parse_toolchain(
        load_yaml_file(get_config_path(TOOLCHAIN_PATH)), where=TOOLCHAIN_PATH
    )
