from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, cast

from guildbotics.app_api.models import CliAgentInfo
from guildbotics.utils.fileio import get_config_path, get_template_path, load_yaml_file

_DEFAULT_ORDER = 1000

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


def _cli_agent_roots(config_dir: Path, person_id: str | None) -> list[Path]:
    """Member, team, and template ``cli_agents/`` roots, in priority order."""
    roots: list[Path] = []
    if person_id:
        roots.append(
            config_dir / "team/members" / person_id / "intelligences/cli_agents"
        )
    roots.append(config_dir / "intelligences/cli_agents")
    roots.append(get_template_path() / "intelligences/cli_agents")
    return roots


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = load_yaml_file(path)
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def discover_cli_agents(
    config_dir: Path, person_id: str | None = None
) -> list[CliAgentInfo]:
    """Discover selectable CLI agents from ``cli_agents/<name>-cli.yml``.

    A CLI agent is any ``*.yml`` (member, team, or template scope); the file in
    the highest-priority scope wins. This is the only place that enumerates the
    CLI agent catalog, so adding an agent is just a matter of dropping in
    ``cli_agents/<name>-cli.yml`` with ``label``/``order``/``executable``.
    """
    files: dict[str, Path] = {}
    for root in _cli_agent_roots(config_dir, person_id):
        if root.is_dir():
            for path in sorted(root.glob("*.yml")):
                name = path.name.removesuffix(".yml").removesuffix("-cli")
                files.setdefault(name, path)

    agents: list[CliAgentInfo] = []
    for name, path in files.items():
        data = _read_yaml(path)
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
    """Return the executable (binary) of the team's default CLI agent."""
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
