"""Every module of the ``guildbotics`` package must be reachable by package walking.

GuildBotics resolves brains and commands from config strings through
``guildbotics.utils.import_utils.load_class``, so the desktop programs cannot rely
on PyInstaller's static import graph: ``desktop/sidecar/guildbotics.spec``
collects the package with ``collect_submodules("guildbotics")`` instead. That
walk, like ``pkgutil.walk_packages``, descends only into regular packages. A
directory without ``__init__.py`` is a namespace package whose modules are
bundled only while some static import happens to reach them, so removing that
import drops them from the build without any test noticing. ``pylint guildbotics``
expands the package the same way, so it never lints such modules either.
"""

from __future__ import annotations

import pkgutil
from pathlib import Path

import guildbotics

PACKAGE_ROOT = Path(guildbotics.__file__).parent
# Command templates are loaded from their file paths and bundled as data files.
TEMPLATES = PACKAGE_ROOT / "templates"


def _raise(name: str) -> None:
    raise ImportError(f"Package '{name}' could not be imported while walking")


def _modules_on_disk() -> set[str]:
    modules: set[str] = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path.is_relative_to(TEMPLATES):
            continue
        parts = path.relative_to(PACKAGE_ROOT).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            modules.add(".".join(("guildbotics", *parts)))
    return modules


def test_every_module_is_reachable_by_package_walk() -> None:
    walked = {
        name
        for _, name, _ in pkgutil.walk_packages(
            guildbotics.__path__, prefix="guildbotics.", onerror=_raise
        )
    }

    assert sorted(_modules_on_disk() - walked) == []
