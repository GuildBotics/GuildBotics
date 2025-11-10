"""Unit tests for guildbotics.utils.i18n_tool.

Validates set/get language behavior and English fallback configuration.
Each test isolates the global i18n state to avoid cross-test interference.
"""

from __future__ import annotations

import ast
import re
from copy import deepcopy
from pathlib import Path
from typing import Generator

import pytest
import yaml  # type: ignore

PLACEHOLDER_PATTERN = re.compile(r"%\{([^}]+)\}")


@pytest.fixture(autouse=True)
def isolate_i18n_state() -> Generator[None, None, None]:
    """Isolate global i18n state per test.

    The i18n library keeps global configuration and load paths. This fixture
    snapshots those structures and restores them after each test so that
    importing and using ``i18n_tool`` in one test does not leak into others.
    """
    import i18n  # type: ignore

    settings_backup = deepcopy(getattr(i18n, "config").settings)
    load_path_backup = list(i18n.load_path)

    try:
        yield
    finally:
        # Restore load path and all config settings
        i18n.load_path[:] = load_path_backup
        i18n.config.settings.clear()
        i18n.config.settings.update(settings_backup)


def test_set_and_get_language_roundtrip() -> None:
    """set_language updates locale and get_language returns it."""
    # Import inside test after the isolation fixture has started
    from guildbotics.utils import i18n_tool

    # Set to a locale (may or may not exist in available_locales)
    i18n_tool.set_language("ja")
    assert i18n_tool.get_language() == "ja"

    # Change to another locale and verify roundtrip
    i18n_tool.set_language("fr")
    assert i18n_tool.get_language() == "fr"


def test_sets_fallback_to_english() -> None:
    """set_language always configures English ('en') as fallback."""
    import i18n

    from guildbotics.utils import i18n_tool

    i18n_tool.set_language("xx")  # arbitrary/unknown locale is fine
    assert i18n.get("fallback") == "en"


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_all_t_call_sites_produce_translated_strings() -> None:
    """Ensure every direct i18n_tool.t() call resolves to a translation."""
    import i18n

    from guildbotics.utils import i18n_tool

    i18n_tool.set_language("en")

    keys = _collect_t_call_keys()
    assert keys, "No i18n_tool.t() call sites were discovered."

    placeholder_map = _build_placeholder_map(locale="en")
    missing_entries = sorted(key for key in keys if key not in placeholder_map)
    assert not missing_entries, f"Missing locale entries for keys: {missing_entries}"

    failures: list[str] = []
    translation_sanity_check = i18n_tool.t("entities.task.available_modes.comment")
    assert (
        translation_sanity_check != "entities.task.available_modes.comment"
    ), translation_sanity_check
    for key in sorted(keys):
        kwargs = {name: f"<{name}>" for name in placeholder_map[key]}
        value = i18n_tool.t(key, **kwargs)
        if value == key:
            raise AssertionError(
                f"Key {key} returned itself. load_path={i18n.load_path}"
            )


def _collect_t_call_keys() -> set[str]:
    """Parse source files and collect string literals passed to i18n_tool.t()."""
    source_root = PROJECT_ROOT / "guildbotics"
    keys: set[str] = set()
    for path in source_root.rglob("*.py"):
        relative = path.relative_to(source_root)
        if relative.parts and relative.parts[0] == "cli":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        aliases = _resolve_t_aliases(tree)
        if not aliases:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in aliases:
                    literal = _extract_literal_key(node)
                    if literal is None:
                        raise AssertionError(
                            f"Non-literal translation key in {path}:{node.lineno}"
                        )
                    keys.add(literal)
    return keys


def _resolve_t_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "guildbotics.utils.i18n_tool"
        ):
            for alias in node.names:
                if alias.name == "t":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _extract_literal_key(node: ast.Call) -> str | None:
    candidate = None
    if node.args:
        candidate = node.args[0]
    else:
        for kw in node.keywords:
            if kw.arg == "key":
                candidate = kw.value
                break
    if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
        return candidate.value
    return None


def _build_placeholder_map(locale: str) -> dict[str, set[str]]:
    placeholder_map: dict[str, set[str]] = {}
    for base_dir in _locale_directories():
        if not base_dir.exists():
            continue
        for path in base_dir.rglob(f"*.{locale}.yml"):
            namespace = _path_to_namespace(base_dir, path, locale)
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            payload = data.get(locale)
            if not payload:
                continue
            for suffix, text in _iter_leaf_strings(payload):
                full_key = ".".join(part for part in (namespace, suffix) if part)
                if not isinstance(text, str):
                    continue
                placeholder_map[full_key] = set(PLACEHOLDER_PATTERN.findall(text))
    return placeholder_map


def _locale_directories() -> list[Path]:
    return [PROJECT_ROOT / "guildbotics" / "templates" / "locales"]


def _path_to_namespace(base_dir: Path, file_path: Path, locale: str) -> str:
    relative = file_path.relative_to(base_dir)
    parts = list(relative.parts)
    filename = parts.pop()
    namespace_parts = parts
    suffix = f".{locale}.yml"
    file_namespace = filename[: -len(suffix)] if filename.endswith(suffix) else filename
    if file_namespace:
        namespace_parts.append(file_namespace)
    return ".".join(namespace_parts)


def _iter_leaf_strings(
    node: object, prefix: tuple[str, ...] = ()
) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            results.extend(_iter_leaf_strings(value, prefix + (str(key),)))
    elif isinstance(node, str):
        results.append((".".join(prefix), node))
    return results
