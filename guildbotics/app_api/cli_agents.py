from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from guildbotics.utils.fileio import get_config_path, load_yaml_file

CliAgentName = Literal["codex", "gemini", "claude", "copilot"]

CLI_AGENT_EXECUTABLES: tuple[CliAgentName, ...] = (
    "codex",
    "gemini",
    "claude",
    "copilot",
)


def load_cli_agent_script(config_root: Path, executable_info_file: str) -> str:
    if not executable_info_file:
        return ""
    try:
        executable_info = cast(
            dict[str, Any],
            load_yaml_file(
                config_root / f"intelligences/cli_agents/{executable_info_file}"
            ),
        )
        return str(executable_info.get("script", ""))
    except Exception:
        return ""


def resolve_cli_executable(script: str) -> str:
    for executable in CLI_AGENT_EXECUTABLES:
        if executable in script:
            return executable
    return ""


def resolve_default_cli_executable() -> str:
    try:
        mapping = cast(
            dict[str, Any],
            load_yaml_file(get_config_path("intelligences/cli_agent_mapping.yml")),
        )
        executable_info_file = str(mapping.get("default", ""))
    except Exception:
        return ""

    script = load_cli_agent_script(get_config_path(""), executable_info_file)
    return resolve_cli_executable(script)
