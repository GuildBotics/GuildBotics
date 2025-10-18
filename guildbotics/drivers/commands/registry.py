from __future__ import annotations

from guildbotics.drivers.commands.command_base import CommandBase
from guildbotics.drivers.commands.errors import CustomCommandError
from guildbotics.drivers.commands.markdown_command import MarkdownCommand
from guildbotics.drivers.commands.print_command import PrintCommand
from guildbotics.drivers.commands.python_command import PythonCommand
from guildbotics.drivers.commands.shell_script_command import ShellScriptCommand

_COMMAND_TYPES: tuple[type[CommandBase], ...] = (
    MarkdownCommand,
    PythonCommand,
    ShellScriptCommand,
    PrintCommand,
)
_COMMAND_REGISTRY: dict[str, type[CommandBase]] = {
    command_type.get_extension().lower(): command_type
    for command_type in _COMMAND_TYPES
    if command_type.is_shortcut_only() is False
}


def get_command_types() -> tuple[type[CommandBase], ...]:
    """Return the tuple of registered command types in registration order."""
    return _COMMAND_TYPES


def get_command_extensions() -> tuple[str, ...]:
    """Return the registered command extensions."""
    return tuple(_COMMAND_REGISTRY.keys())


def find_command_class(extension: str) -> type[CommandBase]:
    """Return the registered command class for the given file extension."""
    extension = extension.lower()
    if extension not in _COMMAND_REGISTRY:
        raise CustomCommandError(f"Unknown command type: '{extension}'.")

    return _COMMAND_REGISTRY[extension]
