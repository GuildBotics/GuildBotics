from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from guildbotics.commands.errors import CommandError
from guildbotics.commands.registry import get_command_extensions
from guildbotics.runtime.context import Context
from guildbotics.utils.fileio import (
    get_primary_config_path,
    get_template_path,
)


def get_shared_commands_root() -> Path:
    """Return the primary (workspace) shared commands directory."""
    return get_primary_config_path(Path("commands"))


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
    single definition of command resolution order; both real and prospective
    resolution pick the first candidate that exists.
    """
    identifier_path = Path(identifier)
    extensions = [""] if identifier_path.suffix else list(get_command_extensions())
    rel_bases: list[str] = []
    if person_id is not None:
        rel_bases.append(f"team/members/{person_id}/commands/{identifier}")
    rel_bases.append(f"commands/{identifier}")

    template_root = get_template_path()
    for extension in extensions:
        for rel_base in rel_bases:
            base = Path(f"{rel_base}{extension}")
            for variant in _locale_variants(base, language_code):
                yield get_primary_config_path(variant)
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


def resolve_prospective_shared_command(
    target: Path, identifier: str, language_code: str
) -> Path | None:
    """Resolve as if ``target`` already existed on disk (shared, person-less).

    Used before creating a new command file to confirm the file would actually
    be the one the runtime executes, rather than being shadowed by a localized
    template or a higher-priority extension.
    """
    resolved_target = target.resolve(strict=False)
    for path in iter_candidate_paths(identifier, language_code, person_id=None):
        if path.exists() or path.resolve(strict=False) == resolved_target:
            return path
    return None


def resolve_named_command(context: Context, identifier: str) -> Path:
    """Resolve a logical command name to an on-disk prompt or script."""
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


def iter_effective_commands(
    candidate_roots: Iterable[Path],
    language_code: str,
    person_id: str | None = None,
) -> Iterator[tuple[str, Path]]:
    """Yield ``(command, effective_path)`` for candidates under the given roots.

    Candidate logical names are collected from ``candidate_roots`` and each is
    resolved through the shared runtime resolver, so the effective file always
    matches what the runtime would execute (including template shadowing).
    """
    for name in iter_command_candidate_names(candidate_roots, language_code):
        resolved = resolve_command_path(name, language_code, person_id)
        if resolved is not None:
            yield name, resolved


def iter_effective_shared_commands(
    language_code: str,
) -> Iterator[tuple[str, Path]]:
    """Yield editor-eligible shared commands.

    Only logical commands whose effective file (as the runtime resolves it) is
    the shared workspace file itself are returned. Commands shadowed by a
    localized template or a higher-priority extension are excluded.
    """
    shared_root = get_shared_commands_root()
    for name in iter_command_candidate_names([shared_root], language_code):
        resolved = resolve_command_path(name, language_code, person_id=None)
        if resolved is not None and is_within(resolved, shared_root):
            yield name, resolved


def command_source(path: Path) -> str:
    """Classify a resolved command path as ``template`` or ``workspace``."""
    return "template" if is_within(path, get_template_path()) else "workspace"


def is_within(path: Path, root: Path) -> bool:
    """Return whether ``path`` resolves to a location under ``root``."""
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


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
