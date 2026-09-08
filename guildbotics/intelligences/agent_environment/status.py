"""What this device holds of the agent environment, and why a turn may not start.

One reading of the device -- the runtime, the shared declaration, the snapshot
built from it, the provider logins -- answers the CLI's ``environment status``,
the Desktop's status card and alert band, and the refusal a turn is given when
it cannot start here. They must not disagree, so they are one function and one
set of words.
"""

from __future__ import annotations

from dataclasses import dataclass

from guildbotics.intelligences.agent_environment import runtime, snapshot
from guildbotics.intelligences.agent_environment.provider_state import is_logged_in
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentHealth,
)
from guildbotics.intelligences.agent_environment.snapshot import SnapshotStatus
from guildbotics.intelligences.agent_environment.toolchain import (
    ToolchainDeclaration,
    ToolchainError,
    load_toolchain,
    upstream_nameservers,
)
from guildbotics.intelligences.cli_agents import CLI_AGENTS, CliAgentInfo


@dataclass(frozen=True, slots=True)
class DnsStatus:
    """The declared upstream resolvers and what they come to on this device."""

    declared: str
    nameservers: tuple[str, ...] = ()
    problem: str = ""


@dataclass(frozen=True, slots=True)
class ToolStatus:
    """One AI CLI tool of the catalog as this device can run it."""

    name: str
    label: str
    provisioned: bool
    logged_in: bool

    @property
    def refusal(self) -> str:
        """Why a turn of this tool cannot start here, or "" when it can."""
        if not self.provisioned:
            return f"{self.label} is not provisioned in the agent environment yet."
        if not self.logged_in:
            return (
                f"{self.label} is not logged in on this device; run "
                f"`guildbotics environment login {self.name}`."
            )
        return ""


@dataclass(frozen=True, slots=True)
class DeviceStatus:
    """Everything a turn needs from this device, as it stands now.

    ``snapshot`` is None when the declaration could not be read: there is
    then no snapshot to ask for, and ``declaration_problem`` says why.
    """

    runtime: AgentEnvironmentHealth
    declaration: ToolchainDeclaration | None
    declaration_problem: str
    snapshot: SnapshotStatus | None
    dns: DnsStatus
    tools: tuple[ToolStatus, ...]

    @property
    def refusal(self) -> str:
        """Why no turn at all can start on this device, or "" when one can.

        A tool's own login is not part of it; see :meth:`tool`.
        """
        if not self.runtime.available:
            return self.runtime.reason
        if self.snapshot is None:
            return self.declaration_problem
        if self.dns.problem:
            return self.dns.problem
        return _not_ready(self.snapshot)

    @property
    def ready(self) -> bool:
        return not self.refusal

    def tool(self, name: str) -> ToolStatus:
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise ValueError(f"'{name}' is not a supported AI CLI tool")


def device_status(*, building_here: bool = False) -> DeviceStatus:
    """Read the device once.

    Args:
        building_here: This process has just started a build whose lock the
            snapshot directory may not show yet; report the snapshot as
            building rather than let the answer flicker.
    """
    health = runtime.doctor()
    try:
        declaration: ToolchainDeclaration | None = load_toolchain()
        declaration_problem = ""
    except ToolchainError as exc:
        declaration = None
        declaration_problem = str(exc)
    state: SnapshotStatus | None = None
    dns = DnsStatus(declared="")
    if declaration is not None:
        state = snapshot.snapshot_status(declaration)
        if building_here and state.state in ("missing", "stale"):
            state = SnapshotStatus("building", state.name, state.path)
        declared = declaration.dns.nameservers
        dns = DnsStatus(
            declared=declared if isinstance(declared, str) else ", ".join(declared)
        )
        try:
            dns = DnsStatus(dns.declared, upstream_nameservers(declaration.dns))
        except ToolchainError as exc:
            dns = DnsStatus(dns.declared, problem=str(exc))
    return DeviceStatus(
        runtime=health,
        declaration=declaration,
        declaration_problem=declaration_problem,
        snapshot=state,
        dns=dns,
        tools=tuple(_tool_status(agent) for agent in CLI_AGENTS),
    )


def _tool_status(agent: CliAgentInfo) -> ToolStatus:
    return ToolStatus(
        name=agent.name,
        label=agent.label,
        provisioned=bool(agent.provision.package),
        logged_in=is_logged_in(agent),
    )


def _not_ready(status: SnapshotStatus) -> str:
    if status.state == "ready":
        return ""
    if status.state == "building":
        return (
            "The agent environment is being built on this device; "
            "try again when it is ready."
        )
    if status.state == "failed":
        return f"The agent environment failed to build on this device: {status.detail}"
    return (
        f"The agent environment is {status.state} on this device; build it with "
        "`guildbotics environment build`."
    )
