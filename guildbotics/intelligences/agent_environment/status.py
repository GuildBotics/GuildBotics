"""What this device holds of the agent environment, and why a turn may not start.

One reading of the device -- the runtime, the shared declaration, the snapshot
built from it, the provider logins -- answers the CLI's ``environment status``,
the Desktop's status card and alert band, and the refusal a turn is given when
it cannot start here. They must not disagree, so they are one function and one
set of words.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from guildbotics.intelligences.agent_environment import runtime, snapshot
from guildbotics.intelligences.agent_environment.contract import (
    AccessContractError,
    ResolvedAccess,
    exchange_dir,
    load_local_grants,
    load_shared_grants,
    resolve_access,
)
from guildbotics.intelligences.agent_environment.provider_state import (
    authentication_failed,
    has_credentials,
)
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
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.processes import launching_app_name

#: Which part of the device a refusal is about, so whoever shows it can point
#: at what to do: the runtime this device lacks, the shared declaration (or
#: the resolvers it names), the snapshot to build, or a build to wait for.
DeviceSetting = Literal["runtime", "declaration", "snapshot", "building", "filesystem"]


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
    credentials_saved: bool
    authentication_failed: bool = False

    @property
    def problem(self) -> str:
        """Current guidance; a past failure does not prevent another attempt."""
        if self.refusal:
            return self.refusal
        if self.authentication_failed:
            return t(
                "intelligences.agent_environment.tool.authentication_failed",
                tool=self.label,
                command=login_command(self.name),
            )
        return ""

    @property
    def refusal(self) -> str:
        """Why a turn of this tool cannot start here, or "" when it can."""
        if not self.provisioned:
            return t(
                "intelligences.agent_environment.tool.not_provisioned", tool=self.label
            )
        if not self.credentials_saved:
            return t(
                "intelligences.agent_environment.tool.credentials_missing",
                tool=self.label,
                command=login_command(self.name),
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
    access: ResolvedAccess = field(default_factory=ResolvedAccess)
    filesystem_problem: str = ""

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
        return _not_ready(self.snapshot) or self.filesystem_problem

    @property
    def setting(self) -> DeviceSetting | Literal[""]:
        """What :attr:`refusal` is about, or "" when nothing refuses."""
        if not self.runtime.available:
            return "runtime"
        if self.snapshot is None or self.dns.problem:
            return "declaration"
        if self.snapshot.state == "building":
            return "building"
        if self.snapshot.state != "ready":
            return "snapshot"
        if self.filesystem_problem:
            return "filesystem"
        return ""

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
    access, filesystem_problem = filesystem_status()
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
        access=access,
        filesystem_problem=filesystem_problem,
    )


def filesystem_status() -> tuple[ResolvedAccess, str]:
    """Resolve grants without creating them and try directory enumeration.

    Missing document grants probe their nearest existing parent, where a turn
    would create them. A stat/exists check alone does not trigger macOS TCC.
    """
    access = ResolvedAccess()
    target = exchange_dir()
    try:
        access = resolve_access(load_shared_grants(), load_local_grants(), create=False)
        for grant in (*access.documents, *(g for g in access.paths if g.present)):
            if any(grant.path.is_relative_to(d.path) for d in access.denied):
                continue
            target = grant.path
            while True:
                try:
                    with os.scandir(target) as entries:
                        next(entries, None)
                    break
                except FileNotFoundError:
                    if target == target.parent:
                        raise
                    target = target.parent
    except PermissionError as exc:
        return access, filesystem_permission_problem(Path(exc.filename or target))
    except AccessContractError as exc:
        return access, str(exc)
    except OSError as exc:
        return access, t(
            "intelligences.agent_environment.filesystem.unavailable",
            path=target,
            error=exc,
        )
    return access, ""


def filesystem_permission_problem(path: Path) -> str:
    """Describe a permission refusal in the same words for every caller."""
    if sys.platform == "darwin" and path.is_relative_to(Path.home() / "Documents"):
        app = launching_app_name()
        return t(
            "intelligences.agent_environment.filesystem.macos_documents",
            app=t("intelligences.agent_environment.filesystem.launching_app", app=app)
            if app
            else "",
        )
    return t("intelligences.agent_environment.filesystem.permission_denied", path=path)


def login_command(name: str, *, platform: str | None = None) -> str:
    """The terminal login instruction shared by status, alerts, and Desktop.

    Windows installers put the CLI on PATH. Unix Desktop installs it under
    home; quote its absolute path so spaces and shell metacharacters survive.
    """
    if (platform or sys.platform) == "win32":
        return f"guildbotics environment login {name}"
    return shlex.join(
        [
            str(Path.home() / ".guildbotics/bin/guildbotics"),
            "environment",
            "login",
            name,
        ]
    )


def _tool_status(agent: CliAgentInfo) -> ToolStatus:
    return ToolStatus(
        name=agent.name,
        label=agent.label,
        provisioned=agent.provision.provisioned,
        credentials_saved=has_credentials(agent),
        authentication_failed=authentication_failed(agent),
    )


def _not_ready(status: SnapshotStatus) -> str:
    if status.state == "ready":
        return ""
    if status.state == "building":
        return t("intelligences.agent_environment.snapshot.building")
    if status.state == "failed":
        return t(
            "intelligences.agent_environment.snapshot.failed", detail=status.detail
        )
    if status.state == "stale":
        return t("intelligences.agent_environment.snapshot.stale")
    return t("intelligences.agent_environment.snapshot.missing")
