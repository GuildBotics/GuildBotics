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


class CliAgentInfo(BaseModel):
    """A selectable AI CLI tool, discovered from ``cli_agents/<name>-cli.yml``.

    Single source of truth for the AI CLI tool catalog: ``name`` is the file stem
    (without ``-cli``), and the rest comes from that file.
    """

    name: str
    label: str = ""
    order: int = 1000
    executable: str = ""


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


def discover_cli_agents(
    config_dir: Path, person_id: str | None = None
) -> list[CliAgentInfo]:
    """Discover selectable AI CLI tools from ``cli_agents/<name>-cli.yml``.

    An AI CLI tool is any ``*.yml`` (member, team, or template scope); the file in
    the highest-priority scope wins. This is the only place that enumerates the
    AI CLI tool catalog, so adding an agent is just a matter of dropping in
    ``cli_agents/<name>-cli.yml`` with ``label``/``order``/``executable``.
    """
    files: dict[str, Path] = {}
    for root in get_intelligence_roots(config_dir, person_id, "cli_agents"):
        if root.is_dir():
            for path in sorted(root.glob("*.yml")):
                name = path.name.removesuffix(".yml").removesuffix("-cli")
                files.setdefault(name, path)

    agents: list[CliAgentInfo] = []
    for name, path in files.items():
        data = load_yaml_dict(path)
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

    default_name = default_file.removesuffix(".yml").removesuffix("-cli")
    for agent in discover_cli_agents(get_config_path("")):
        if agent.name == default_name:
            return agent.executable
    return ""
