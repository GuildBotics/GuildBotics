from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from guildbotics.utils.fileio import (
    get_config_path,
    get_intelligence_roots,
    load_yaml_dict,
    load_yaml_file,
)

_DEFAULT_ORDER = 1000
#: Every AI CLI tool lives at ``cli_agents/<tool>/default.yml`` and a slot may
#: add ``cli_agents/<tool>/<slot>.yml`` beside it -- the same two-level shape
#: model definitions use, so both sides derive identity from the directory.
CLI_AGENT_DEFAULT_FILENAME = "default.yml"
CLI_AGENT_ROOT = "cli_agents"
#: ``cli_agents/<tool>/<slot>.yml``
_CLI_AGENT_PATH_PARTS = 3
_CLI_AGENT_TOOL_INDEX = 1


class CliAgentInfo(BaseModel):
    """A selectable native or YAML-configured AI CLI tool."""

    name: str
    label: str = ""
    order: int = 1000
    executable: str = ""
    config_reference: str = ""


_NATIVE_AGENTS = (
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
)
_NATIVE_AGENT_NAMES = frozenset(agent.name for agent in _NATIVE_AGENTS)


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


def is_native_cli_agent(name: str) -> bool:
    """Whether this tool is driven by a built-in adapter rather than a script."""
    return name in _NATIVE_AGENT_NAMES


def cli_agent_default_path(name: str) -> str:
    """The definition path of a tool's own default."""
    return f"{CLI_AGENT_ROOT}/{name}/{CLI_AGENT_DEFAULT_FILENAME}"


def _read_cli_agent_default(roots: list[Path], name: str) -> dict[str, Any]:
    for root in roots:
        data = load_yaml_dict(root / name / CLI_AGENT_DEFAULT_FILENAME)
        if data:
            return data
    return {}


def discover_cli_agents(
    config_dir: Path, person_id: str | None = None
) -> list[CliAgentInfo]:
    """Discover native and YAML-configured selectable AI CLI tools.

    A tool is any directory holding a ``default.yml`` (member, team, or template
    scope); the file in the highest-priority scope wins. Only that file names a
    tool, so the per-slot definitions beside it are never mistaken for tools of
    their own. This is the only place that enumerates the catalog, so adding a
    one-shot tool means dropping in ``cli_agents/<name>/default.yml`` with
    ``label`` / ``order`` / ``executable``.
    """
    roots = get_intelligence_roots(config_dir, person_id, CLI_AGENT_ROOT)
    names: set[str] = set()
    for root in roots:
        if root.is_dir():
            for child in root.iterdir():
                if child.is_dir() and (child / CLI_AGENT_DEFAULT_FILENAME).exists():
                    names.add(child.name)

    agents: list[CliAgentInfo] = list(_NATIVE_AGENTS)
    for name in names:
        if is_native_cli_agent(name):
            continue
        data = _read_cli_agent_default(roots, name)
        try:
            order = int(data.get("order", _DEFAULT_ORDER))
        except (TypeError, ValueError):
            order = _DEFAULT_ORDER
        agents.append(
            CliAgentInfo(
                name=name,
                label=str(data.get("label", "") or name),
                order=order,
                executable=str(data.get("executable", "") or name),
                config_reference=cli_agent_default_path(name),
            )
        )
    agents.sort(key=lambda agent: (agent.order, agent.name))
    return agents


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
    for agent in discover_cli_agents(get_config_path("")):
        if agent.name == default_name:
            return agent.executable
    return ""
