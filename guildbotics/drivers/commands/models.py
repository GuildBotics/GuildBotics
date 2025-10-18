from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from guildbotics.utils.import_utils import ClassResolver

if TYPE_CHECKING:
    from guildbotics.drivers.commands.command_base import CommandBase


@dataclass
class CommandSpec:
    """Normalized representation of a command or error handler definition."""

    name: str
    base_dir: Path
    command_class: type[CommandBase]
    path: Path | None = None
    params: dict[str, Any] = field(default_factory=dict)
    args: list[Any] | None = None
    stdin_override: str | None = None
    children: list["CommandSpec"] = field(default_factory=list)
    cwd: Path = Path.cwd()
    command_index: int = 0
    config: dict[str, Any] = field(default_factory=dict)
    class_resolver: ClassResolver | None = None

    def get_config_value(self, key: str, default: Any = None) -> Any:
        if key in self.config:
            return self.config[key]
        return default


@dataclass
class CommandOutcome:
    result: Any
    text_output: str


@dataclass
class InvocationOptions:
    args: list[Any]
    message: str
    params: dict[str, Any]
    output_key: str
