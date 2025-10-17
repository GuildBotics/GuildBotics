from __future__ import annotations

from typing import Mapping, Type

from guildbotics.drivers.commands.command_base import CommandBase
from guildbotics.drivers.commands.markdown_command import MarkdownCommand
from guildbotics.drivers.commands.python_command import PythonCommand
from guildbotics.drivers.commands.shell_script_command import ShellScriptCommand


_COMMAND_TYPES: tuple[Type[CommandBase], ...] = (
    MarkdownCommand,
    PythonCommand,
    ShellScriptCommand,
)
_COMMAND_REGISTRY: dict[str, Type[CommandBase]] = {
    command_type.get_extension(): command_type for command_type in _COMMAND_TYPES
}


def get_command_types() -> tuple[Type[CommandBase], ...]:
    """Return the tuple of registered command types in registration order."""
    return _COMMAND_TYPES


def get_command_registry() -> Mapping[str, Type[CommandBase]]:
    """Return a mapping of file extensions to command classes."""
    return _COMMAND_REGISTRY


def get_command_extensions() -> tuple[str, ...]:
    """Return the registered command extensions."""
    return tuple(_COMMAND_REGISTRY.keys())


def find_command_class(extension: str) -> Type[CommandBase] | None:
    """Return the registered command class for the given file extension."""
    return _COMMAND_REGISTRY.get(extension)
