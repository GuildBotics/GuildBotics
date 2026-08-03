from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from guildbotics.utils.fileio import get_config_path, load_yaml_file

#: Every AI CLI tool lives at ``cli_agents/<tool>/default.yml`` and a slot may
#: add ``cli_agents/<tool>/<slot>.yml`` beside it -- the same two-level shape
#: model definitions use, so both sides derive identity from the directory.
CLI_AGENT_DEFAULT_FILENAME = "default.yml"
CLI_AGENT_ROOT = "cli_agents"
#: ``cli_agents/<tool>/<slot>.yml``
_CLI_AGENT_PATH_PARTS = 3
_CLI_AGENT_TOOL_INDEX = 1


class CliAgentInfo(BaseModel):
    """A selectable AI CLI tool."""

    name: str
    label: str = ""
    order: int = 1000
    executable: str = ""
    config_reference: str = ""


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
    ),
    CliAgentInfo(
        name="claude",
        label="Claude Code",
        order=20,
        executable="claude",
        config_reference=f"{CLI_AGENT_ROOT}/claude/{CLI_AGENT_DEFAULT_FILENAME}",
    ),
    CliAgentInfo(
        name="grok",
        label="Grok Build",
        order=30,
        executable="grok",
        config_reference=f"{CLI_AGENT_ROOT}/grok/{CLI_AGENT_DEFAULT_FILENAME}",
    ),
    CliAgentInfo(
        name="copilot",
        label="GitHub Copilot",
        order=40,
        executable="copilot",
        config_reference=f"{CLI_AGENT_ROOT}/copilot/{CLI_AGENT_DEFAULT_FILENAME}",
    ),
    CliAgentInfo(
        name="antigravity",
        label="Antigravity",
        order=50,
        executable="agy",
        config_reference=f"{CLI_AGENT_ROOT}/antigravity/{CLI_AGENT_DEFAULT_FILENAME}",
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


def get_cli_agent_search_path(path: str | None = None) -> str:
    current = os.environ.get("PATH") if path is None else path
    if path is not None and current == "":
        return ""
    entries = [entry for entry in (current or os.defpath).split(os.pathsep) if entry]
    home = Path.home()
    entries.extend(
        [
            str(home / ".guildbotics/bin"),
            str(home / ".local/bin"),
            str(home / "bin"),
            str(home / ".cargo/bin"),
            str(home / ".volta/bin"),
        ]
    )
    entries.extend(GUI_APP_PATHS)
    return os.pathsep.join(dict.fromkeys(entries))


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

    default_name = cli_agent_name_from_path(default_file)
    for agent in CLI_AGENTS:
        if agent.name == default_name:
            return agent.executable
    return ""
