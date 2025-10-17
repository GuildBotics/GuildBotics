from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from guildbotics.utils.import_utils import ClassResolver


@dataclass
class CommandSpec:
    """Normalized representation of a command or error handler definition."""

    name: str
    path: Path
    params: dict[str, Any] = field(default_factory=dict)
    args: list[Any] | None = None
    stdin_override: str | None = None
    base_dir: Path | None = None
    children: list["CommandSpec"] = field(default_factory=list)
    metadata: dict[str, Any] | None = None
    cwd: Path = Path.cwd()
    command_index: int = 0
    config: dict | None = None
    class_resolver: ClassResolver | None = None

    @property
    def kind(self) -> str:
        return self.path.suffix.lower()

    def get_config_value(self, key: str, default: Any = None) -> Any:
        if self.config and key in self.config:
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
