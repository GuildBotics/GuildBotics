from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from guildbotics.commands.errors import CommandError

_ARGUMENT_NAME_PATTERN = re.compile(r"[A-Za-z_]\w*|\d+")
_ARGUMENT_KEYS = {"default", "required"}


@dataclass(frozen=True)
class CommandArgumentDefinition:
    """A declared Markdown or YAML command argument."""

    name: str
    required: bool
    default: str | None


def parse_command_argument_definitions(
    config: dict[str, Any],
) -> list[CommandArgumentDefinition]:
    """Parse root-level argument declarations from command metadata."""
    raw_definitions = config.get("args")
    if raw_definitions is None:
        return []
    if not isinstance(raw_definitions, dict):
        raise CommandError("Command 'args' must be a mapping.")

    definitions: list[CommandArgumentDefinition] = []
    for raw_name, raw_definition in raw_definitions.items():
        name = str(raw_name)
        if _ARGUMENT_NAME_PATTERN.fullmatch(name) is None:
            raise CommandError(f"Invalid command argument name: {name!r}.")
        if not isinstance(raw_definition, dict):
            raise CommandError(f"Command argument '{name}' must be a mapping.")

        unknown_keys = set(raw_definition) - _ARGUMENT_KEYS
        if unknown_keys:
            unknown = ", ".join(sorted(str(key) for key in unknown_keys))
            raise CommandError(
                f"Command argument '{name}' has unsupported fields: {unknown}."
            )

        has_default = "default" in raw_definition
        raw_default = raw_definition.get("default")
        if has_default and (
            raw_default is None or isinstance(raw_default, (dict, list))
        ):
            raise CommandError(
                f"Command argument '{name}' default must be a scalar value."
            )

        required = raw_definition.get("required", not has_default)
        if not isinstance(required, bool):
            raise CommandError(
                f"Command argument '{name}' required must be true or false."
            )
        if required and has_default:
            raise CommandError(
                f"Command argument '{name}' cannot be required and have a default."
            )
        definitions.append(
            CommandArgumentDefinition(
                name=name,
                required=required,
                default=str(raw_default) if has_default else None,
            )
        )
    return definitions


def resolve_command_argument_params(
    params: dict[str, Any], definitions: list[CommandArgumentDefinition]
) -> dict[str, Any]:
    """Apply declared defaults and reject missing required arguments."""
    resolved = dict(params)
    missing: list[str] = []
    for definition in definitions:
        value = resolved.get(definition.name)
        empty = value is None or (isinstance(value, str) and not value.strip())
        if not empty:
            continue
        if definition.default is not None:
            resolved[definition.name] = definition.default
        elif definition.required:
            missing.append(definition.name)

    if missing:
        names = ", ".join(missing)
        raise CommandError(f"Missing required command arguments: {names}.")
    return resolved
