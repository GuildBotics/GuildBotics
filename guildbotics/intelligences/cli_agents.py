from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

from guildbotics.intelligences.sandbox import NETWORK_MODES, NetworkPolicy
from guildbotics.utils.fileio import get_config_path, load_yaml_file

#: Every AI CLI tool lives at ``cli_agents/<tool>/default.yml`` and a slot may
#: add ``cli_agents/<tool>/<slot>.yml`` beside it -- the same two-level shape
#: model definitions use, so both sides derive identity from the directory.
CLI_AGENT_DEFAULT_FILENAME = "default.yml"
CLI_AGENT_ROOT = "cli_agents"
#: ``cli_agents/<tool>/<slot>.yml``
_CLI_AGENT_PATH_PARTS = 3
_CLI_AGENT_TOOL_INDEX = 1


#: The platform key that stands for every OS in a per-OS mode table.
ANY_PLATFORM = "*"
_ALL_MODES = frozenset(NETWORK_MODES)


class CliAgentNetworkSupport(BaseModel):
    """Which parts of the network contract a tool can enforce natively.

    Enforcement is what the provider's own sandbox does, not what its prompt
    or permission rules ask for. A mode that is missing here is one the
    adapter refuses to start with, never one it quietly widens.
    """

    model_config = ConfigDict(frozen=True)

    #: Command modes per ``sys.platform`` value; ``ANY_PLATFORM`` is the default.
    command_modes: dict[str, frozenset[str]] = {ANY_PLATFORM: _ALL_MODES}
    #: Modes the built-in web tools can be held to.
    web_modes: frozenset[str] = _ALL_MODES
    #: Command modes under which local network reachability is a separate
    #: switch (``allow_local_network``); elsewhere it follows the mode itself.
    local_network_modes: frozenset[str] = frozenset()
    #: Grant accesses the tool can hold commands and its file tools to.
    grant_accesses: frozenset[str] = frozenset({"read", "read_write"})
    #: Whether the adapter translates the sandbox contract at all. Until it
    #: does, the tool runs under its previous fixed settings and the contract
    #: is neither validated nor reported for it.
    contract_applied: bool = False

    def command_modes_on(self, platform: str | None) -> frozenset[str]:
        """Modes enforceable on one platform, or on any platform when None."""
        if platform is None:
            return frozenset().union(*self.command_modes.values())
        return self.command_modes.get(
            platform, self.command_modes.get(ANY_PLATFORM, frozenset())
        )


class CliAgentInfo(BaseModel):
    """A selectable AI CLI tool."""

    name: str
    label: str = ""
    order: int = 1000
    executable: str = ""
    config_reference: str = ""
    network: CliAgentNetworkSupport = CliAgentNetworkSupport()


#: The complete catalog of selectable AI CLI tools, in display order. Every tool
#: is driven by a built-in adapter, so supporting a new one means implementing
#: an adapter here rather than dropping a YAML file into a workspace.
CLI_AGENTS: tuple[CliAgentInfo, ...] = (
    CliAgentInfo(
        name="codex",
        label="Codex",
        order=10,
        executable="codex",
        config_reference=f"{CLI_AGENT_ROOT}/codex/{CLI_AGENT_DEFAULT_FILENAME}",
        # Commands: permission profile + network proxy. Web search: its own
        # domain filter. Local reachability is a proxy switch, so it only
        # exists once the proxy (allowlist) is on. The native Windows sandbox
        # reads only fixed machine-wide roots (openai/codex#27171) and has not
        # been verified to honour the profile's read paths or the proxy, so
        # nothing is claimed there until it is.
        network=CliAgentNetworkSupport(
            command_modes={"darwin": _ALL_MODES, "linux": _ALL_MODES},
            local_network_modes=frozenset({"allowlist"}),
            contract_applied=True,
        ),
    ),
    CliAgentInfo(
        name="claude",
        label="Claude Code",
        order=20,
        executable="claude",
        config_reference=f"{CLI_AGENT_ROOT}/claude/{CLI_AGENT_DEFAULT_FILENAME}",
        # Bash sandbox with a strict domain allowlist; WebFetch by permission
        # rule. The sandbox proxy separates local addresses in every mode
        # that restricts anything.
        network=CliAgentNetworkSupport(
            local_network_modes=frozenset({"deny", "allowlist"})
        ),
    ),
    CliAgentInfo(
        name="grok",
        label="Grok Build",
        order=30,
        executable="grok",
        config_reference=f"{CLI_AGENT_ROOT}/grok/{CLI_AGENT_DEFAULT_FILENAME}",
        # Child-process network is a seccomp filter, so only Linux can close
        # it; macOS commands always reach the network. WebFetch takes a
        # domain rule, and WebSearch is switched off under a web allowlist.
        network=CliAgentNetworkSupport(
            command_modes={
                "linux": frozenset({"deny", "unrestricted"}),
                ANY_PLATFORM: frozenset({"unrestricted"}),
            }
        ),
    ),
    CliAgentInfo(
        name="copilot",
        label="GitHub Copilot",
        order=40,
        executable="copilot",
        config_reference=f"{CLI_AGENT_ROOT}/copilot/{CLI_AGENT_DEFAULT_FILENAME}",
        # The local sandbox switches outbound and local network on or off; its
        # URL rules decide prompts, not what the sandbox lets through.
        network=CliAgentNetworkSupport(
            command_modes={ANY_PLATFORM: frozenset({"deny", "unrestricted"})},
            web_modes=frozenset({"deny", "unrestricted"}),
            local_network_modes=frozenset({"deny"}),
        ),
    ),
    CliAgentInfo(
        name="antigravity",
        label="Antigravity",
        order=50,
        executable="agy",
        config_reference=f"{CLI_AGENT_ROOT}/antigravity/{CLI_AGENT_DEFAULT_FILENAME}",
        # The terminal sandbox has no command domain list, and how far it
        # closes the network is unconfirmed, so only `unrestricted` is claimed
        # for commands until that is measured. URL tools take domain rules.
        # Its terminal sandbox takes extra directories only as read/write
        # workspace roots, so a read-only grant has no spelling.
        network=CliAgentNetworkSupport(
            command_modes={ANY_PLATFORM: frozenset({"unrestricted"})},
            grant_accesses=frozenset({"read_write"}),
        ),
    ),
)


GUI_APP_PATHS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def cli_agent_info(name: str) -> CliAgentInfo:
    """The catalog entry for a tool name."""
    for agent in CLI_AGENTS:
        if agent.name == name:
            return agent
    raise ValueError(f"'{name}' is not a supported AI CLI tool")


def unsupported_grant_reason(tool: str, access: str) -> str:
    """Why a tool could not hold commands to a grant, or "" when it can."""
    info = cli_agent_info(tool)
    if access in info.network.grant_accesses:
        return ""
    return f"{info.label} cannot grant '{access}' access to a directory."


def unsupported_network_reason(
    tool: str, network: NetworkPolicy, platform: str | None
) -> str:
    """Why a tool could not enforce a network policy, or "" when it can.

    ``platform`` is a ``sys.platform`` value for the device about to run the
    agent, or None to ask whether any supported OS could enforce it -- the
    question a shared definition is validated against when it is saved.
    """
    info = cli_agent_info(tool)
    where = "on this OS" if platform else "on any OS"
    support = info.network
    command = network.command
    if platform and not support.command_modes_on(platform):
        return f"{info.label} has not been verified to enforce the sandbox on this OS."
    if command.mode not in support.command_modes_on(platform):
        return (
            f"{info.label} cannot enforce command network mode "
            f"'{command.mode}' {where}."
        )
    if command.allow_local_network and command.mode not in support.local_network_modes:
        return (
            f"{info.label} cannot open the local network separately under "
            f"command network mode '{command.mode}'."
        )
    if network.web.mode not in support.web_modes:
        return f"{info.label} cannot enforce web network mode '{network.web.mode}'."
    return ""


def get_cli_agent_search_path(path: str | None = None) -> str:
    current = os.environ.get("PATH") if path is None else path
    if path is not None and current == "":
        return ""
    home = Path.home()
    entries = [
        str(home / ".guildbotics/bin"),
        *[entry for entry in (current or os.defpath).split(os.pathsep) if entry],
        *[
            str(home / ".local/bin"),
            str(home / "bin"),
            str(home / ".cargo/bin"),
            str(home / ".volta/bin"),
        ],
    ]
    entries.extend(GUI_APP_PATHS)
    unique: dict[str, str] = {}
    for entry in entries:
        key = os.path.normcase(os.path.normpath(entry))
        unique.setdefault(key, entry)
    return os.pathsep.join(unique.values())


def resolve_cli_agent_path(executable: str, path: str | None = None) -> str:
    if not executable:
        return ""
    return shutil.which(executable, path=get_cli_agent_search_path(path)) or ""


def cli_agent_name_from_path(path: str) -> str:
    """Return the tool a definition path belongs to.

    Identity is the directory, exactly as a model definition's provider is
    (``models/<provider>/<slot>.yml``), so one tool has one spelling.
    """
    parts = path.split("/")
    if len(parts) < _CLI_AGENT_PATH_PARTS or parts[0] != CLI_AGENT_ROOT:
        return ""
    return parts[_CLI_AGENT_TOOL_INDEX]


def cli_agent_default_path(name: str) -> str:
    """The definition path of a tool's own default."""
    return f"{CLI_AGENT_ROOT}/{name}/{CLI_AGENT_DEFAULT_FILENAME}"


def require_cli_agent_path(path: str, *, where: str) -> str:
    """Return the catalog tool a definition path names.

    The catalog is closed: a tool outside ``CLI_AGENTS`` has no adapter and
    cannot run, so every boundary that accepts a mapping rejects such a path
    here -- with a message naming the mapping entry -- instead of failing deep
    inside a turn with an unknown-adapter error.

    Args:
        path: A definition path such as ``cli_agents/<tool>/<slot>.yml``.
        where: The mapping entry to name in the error message.

    Returns:
        The tool name the path belongs to.

    Raises:
        ValueError: If the path does not name a catalog tool.
    """
    tool = cli_agent_name_from_path(path)
    if any(agent.name == tool for agent in CLI_AGENTS):
        return tool
    supported = ", ".join(agent.name for agent in CLI_AGENTS)
    raise ValueError(
        f"{where}: '{path}' does not name a supported AI CLI tool"
        f" (supported: {supported})"
    )


def cli_agent_executable(name: str) -> str:
    """Return the executable name for a catalog AI CLI tool."""
    for agent in CLI_AGENTS:
        if agent.name == name:
            return agent.executable
    return ""


def resolve_default_cli_executable() -> str:
    """Return the executable (binary) of the team's default AI CLI tool."""
    try:
        mapping = cast(
            dict[str, Any],
            load_yaml_file(get_config_path("intelligences/cli_agent_mapping.yml")),
        )
        default_file = str(mapping.get("default", ""))
    except Exception:
        return ""

    return cli_agent_executable(cli_agent_name_from_path(default_file))
