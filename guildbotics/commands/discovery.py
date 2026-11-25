from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from guildbotics.commands.errors import CommandError
from guildbotics.commands.registry import get_command_extensions
from guildbotics.runtime.context import Context
from guildbotics.utils.fileio import (
    get_primary_config_dir,
    get_template_path,
)
from guildbotics.utils.person_id import person_config_directory


def is_command_source_path(path: Path) -> bool:
    """Return whether ``path`` names a supported command source file."""
    stem = path.with_suffix("").name
    is_reserved_metadata = stem.endswith(".metadata") or ".metadata." in stem
    return path.suffix.lower() in get_command_extensions() and not is_reserved_metadata


def iter_candidate_paths(
    identifier: str, language_code: str, person_id: str | None = None
) -> Iterator[Path]:
    """Yield candidate paths in strict runtime resolution order.

    Precedence is extension-outer (command registry order); inside each
    extension, member roots (when ``person_id`` is given) precede the shared
    roots, and each location is tried by locale in ``<language>`` → ``en`` →
    unsuffixed order across the workspace and package template. This is the
    single definition of command resolution order.
    """
    identifier_path = Path(identifier)
    extensions = [""] if identifier_path.suffix else list(get_command_extensions())
    rel_bases: list[str] = []
    if person_id is not None:
        rel_bases.append(
            (person_config_directory(person_id) / "commands" / identifier).as_posix()
        )
    rel_bases.append(f"commands/{identifier}")

    primary_dir = get_primary_config_dir()
    template_root = get_template_path()
    for extension in extensions:
        for rel_base in rel_bases:
            base = Path(f"{rel_base}{extension}")
            for variant in _locale_variants(base, language_code):
                if primary_dir is not None:
                    yield primary_dir / variant
                yield template_root / variant


def _locale_variants(base: Path, language_code: str) -> list[Path]:
    variants: list[Path] = []
    if language_code:
        variants.append(base.with_stem(f"{base.stem}.{language_code}"))
        variants.append(base.with_stem(f"{base.stem}.en"))
    variants.append(base)
    return variants


def resolve_command_path(
    identifier: str, language_code: str, person_id: str | None = None
) -> Path | None:
    """Resolve a logical command name using the runtime precedence.

    Args:
        identifier: Logical command name, optionally with an explicit suffix.
        language_code: Active project language code.
        person_id: Member to resolve for, or ``None`` for shared resolution.

    Returns:
        The resolved on-disk path, or ``None`` when the command is not found.
    """
    for path in iter_candidate_paths(identifier, language_code, person_id):
        if path.exists() and is_command_source_path(path):
            return path
    return None


def resolve_named_command(context: Context, identifier: str) -> Path:
    """Resolve a logical command name to an on-disk prompt or script."""
    if context.resolved_commands is not None:
        path = context.resolved_commands.get(identifier)
        if path is None:
            raise CommandError(f"Command '{identifier}' was not resolved by the host.")
        return Path(path)
    language_code = context.team.project.get_language_code()
    resolved = resolve_command_path(identifier, language_code, context.person.person_id)
    if resolved is None:
        raise CommandError(f"Unable to locate command '{identifier}'.")
    return resolved


def iter_command_candidate_names(
    roots: Iterable[Path], language_code: str
) -> list[str]:
    """Collect logical command names present under the given physical roots.

    Files whose locale suffix does not match the active language or English are
    skipped, as are unsupported extensions and reserved metadata files.
    Names are de-duplicated while preserving discovery order.
    """
    names: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or not is_command_source_path(path):
                continue
            name = logical_command_name(root, path, language_code)
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def logical_command_name(root: Path, path: Path, language_code: str) -> str:
    """Return the logical command name for a file under ``root``.

    Returns an empty string for files whose locale suffix does not match the
    active language or English.
    """
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if not parts:
        return ""
    stem = parts[-1]
    if "." in stem:
        base, locale = stem.rsplit(".", 1)
        if locale not in {language_code, "en"}:
            return ""
        parts[-1] = base
    return "/".join(parts)


def resolve_command_reference(base_dir: Path, value: str, context: Context) -> Path:
    """Resolve a command reference relative to a base directory or by name."""
    if context.resolved_commands is not None:
        if value in context.resolved_commands:
            return Path(context.resolved_commands[value])
        resolved_anchor = str((base_dir / value).resolve())
        if resolved_anchor in context.resolved_commands.values():
            return Path(resolved_anchor)
        raise CommandError(f"Command '{value}' was not resolved by the host.")
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
