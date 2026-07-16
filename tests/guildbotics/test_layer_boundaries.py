"""Guard the layered architecture of the ``guildbotics`` package.

``guildbotics.app_api`` is the desktop-facing API layer that sits on top of
the core packages (capabilities, drivers, intelligences, observability, ...).
Core modules therefore must never import from ``guildbotics.app_api``; domain
knowledge needed by both sides belongs in the core layer, with ``app_api``
converting it into API response models.

``guildbotics.observability`` is a recording foundation and may only depend on
``guildbotics.utils``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import guildbotics

PACKAGE_ROOT = Path(guildbotics.__file__).parent
REPOSITORY_ROOT = PACKAGE_ROOT.parent


def _imports_by_module(subpackage: str, inside: bool) -> dict[Path, set[str]]:
    """Collect guildbotics imports per module, inside or outside ``subpackage``."""
    imports: dict[Path, set[str]] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT)
        if (relative.parts[0] == subpackage) != inside:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        imports[relative] = {
            module for module in modules if module.startswith("guildbotics")
        }
    return imports


def _matches(module: str, packages: tuple[str, ...]) -> bool:
    return any(
        module == package or module.startswith(f"{package}.") for package in packages
    )


def test_core_modules_do_not_import_app_api() -> None:
    offenders = [
        f"{relative}: {module}"
        for relative, modules in _imports_by_module("app_api", inside=False).items()
        for module in sorted(modules)
        if _matches(module, ("guildbotics.app_api",))
    ]
    assert offenders == []


def test_observability_depends_only_on_utils() -> None:
    allowed = ("guildbotics.observability", "guildbotics.utils")
    offenders = [
        f"{relative}: {module}"
        for relative, modules in _imports_by_module(
            "observability", inside=True
        ).items()
        for module in sorted(modules)
        if not _matches(module, allowed)
    ]
    assert offenders == []


def test_native_provider_wire_protocol_does_not_leak_into_app_or_frontend() -> None:
    wire_tokens = (
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "execCommandApproval",
        "applyPatchApproval",
        '"workspaceWrite"',
        '"dangerFullAccess"',
    )
    roots = (PACKAGE_ROOT / "app_api", REPOSITORY_ROOT / "desktop/src")
    offenders: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".py", ".ts", ".tsx"}:
                continue
            contents = path.read_text(encoding="utf-8")
            offenders.extend(
                f"{path.relative_to(REPOSITORY_ROOT)}: {token}"
                for token in wire_tokens
                if token in contents
            )
    assert offenders == []
