from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from guildbotics.commands.command_base import CommandBase

# Lazy caches populated on first access
_COMMAND_TYPES: tuple[type["CommandBase"], ...] | None = None
_COMMAND_REGISTRY: dict[str, type["CommandBase"]] | None = None


def _ensure_registry() -> None:
    """Populate command type registry on first use with lazy imports.

    This avoids import-time circular dependencies by importing concrete
    command classes only when needed.
    """
    global _COMMAND_TYPES, _COMMAND_REGISTRY
    if _COMMAND_TYPES is not None and _COMMAND_REGISTRY is not None:
        return

    # Import concrete command classes lazily to avoid circular imports
    from guildbotics.commands.markdown_command import MarkdownCommand
    from guildbotics.commands.print_command import PrintCommand
    from guildbotics.commands.python_command import PythonCommand
    from guildbotics.commands.shell_script_command import ShellScriptCommand
    from guildbotics.commands.to_html_command import ToHtmlCommand
    from guildbotics.commands.to_pdf_command import ToPdfCommand
    from guildbotics.commands.yaml_command import YamlCommand

    _COMMAND_TYPES = (
        MarkdownCommand,
        PythonCommand,
        ShellScriptCommand,
        PrintCommand,
        ToHtmlCommand,
        ToPdfCommand,
        YamlCommand,
    )
    _COMMAND_REGISTRY = {
        ext.lower(): command_type
        for command_type in _COMMAND_TYPES
        for ext in command_type.get_extensions()
        if not command_type.is_inline_only()
    }


def get_command_types() -> tuple[type["CommandBase"], ...]:
    """Return the tuple of registered command types in registration order."""
    _ensure_registry()
    # mypy/pylance: _COMMAND_TYPES is ensured non-None after _ensure_registry
    assert _COMMAND_TYPES is not None
    return _COMMAND_TYPES


def get_command_extensions() -> tuple[str, ...]:
    """Return the registered command extensions."""
    _ensure_registry()
    assert _COMMAND_REGISTRY is not None
    return tuple(_COMMAND_REGISTRY.keys())


def find_command_class(extension: str) -> type["CommandBase"]:
    """Return the registered command class for the given file extension."""
    from guildbotics.commands.errors import CommandError

    _ensure_registry()
    extension = extension.lower()
    registry = _COMMAND_REGISTRY
    assert registry is not None
    if extension not in registry:
        raise CommandError(f"Unknown command type: '{extension}'.")

    return registry[extension]
