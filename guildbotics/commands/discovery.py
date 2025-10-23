from __future__ import annotations

from pathlib import Path

from guildbotics.commands.errors import CommandError
from guildbotics.commands.registry import get_command_extensions
from guildbotics.runtime.context import Context
from guildbotics.utils.fileio import get_person_config_path


def resolve_named_command(context: Context, identifier: str) -> Path:
    """Resolve a logical command name to an on-disk prompt or script."""
    language_code = context.team.project.get_language_code()
    person_id = context.person.person_id
    identifier_path = Path(identifier)

    if identifier_path.suffix:
        suffixes = [identifier]
    else:
        suffixes = [
            f"{identifier}{extension}" for extension in get_command_extensions()
        ]

    for suffix_identifier in suffixes:
        prompts_path = get_person_config_path(
            person_id, f"commands/{suffix_identifier}", language_code
        )
        if prompts_path.exists():
            return prompts_path

    raise CommandError(f"Unable to locate command '{identifier}'.")


def resolve_command_reference(base_dir: Path, value: str, context: Context) -> Path:
    """Resolve a command reference relative to a base directory or by name."""
    candidate_path = Path(value)
    if candidate_path.is_absolute():
        if not candidate_path.exists():
            raise CommandError(f"Command file '{candidate_path}' not found.")
        return candidate_path

    anchored = (base_dir / candidate_path).resolve()
    if candidate_path.suffix:
        if anchored.exists():
            return anchored
    else:
        for extension in get_command_extensions():
            extended = anchored.with_suffix(extension)
            if extended.exists():
                return extended

    if anchored.exists():
        return anchored

    resolved = resolve_named_command(context, value)
    if resolved.exists():
        return resolved

    raise CommandError(f"Command '{value}' could not be resolved.")
