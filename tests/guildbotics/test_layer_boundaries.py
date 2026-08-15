"""Guard the layered architecture of the ``guildbotics`` package.

``guildbotics.app_api`` is the desktop-facing API layer that sits on top of
the core packages (capabilities, drivers, intelligences, observability, ...).
Core modules therefore must never import from ``guildbotics.app_api``; domain
knowledge needed by both sides belongs in the core layer, with ``app_api``
converting it into API response models.

``guildbotics.observability`` is a recording foundation and may only depend on
``guildbotics.utils``.

``guildbotics.workspace`` owns workspace storage -- identity, shared-record
schemas, and config revisions -- and sits below the capability, driver, and API
layers, so it may depend only on ``guildbotics.utils`` and ``guildbotics.entities``.

``guildbotics.hub`` owns the machine that hosts synchronization repositories and
how a device reaches one over OpenSSH. It knows nothing about what those
repositories contain, so it may depend only on ``guildbotics.utils``.

``guildbotics.sync`` turns announced shared writes into Git work. It sits above
workspace storage and observability and below everything else: capabilities,
drivers, integrations, and the API layer must reach it only through the
Workspace Sync Port, never by importing it. The exception is the composition
roots below, which are the process entry points that install the queue -- one
per long-lived process. Wiring an implementation is what a composition root is
for; reaching around the port from anywhere else is what the rule forbids.
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


def test_workspace_storage_depends_only_on_utils_and_entities() -> None:
    allowed = (
        "guildbotics.workspace",
        "guildbotics.utils",
        "guildbotics.entities",
    )
    offenders = [
        f"{relative}: {module}"
        for relative, modules in _imports_by_module("workspace", inside=True).items()
        for module in sorted(modules)
        if not _matches(module, allowed)
    ]
    assert offenders == []


def test_hub_depends_only_on_utils() -> None:
    allowed = ("guildbotics.hub", "guildbotics.utils")
    offenders = [
        f"{relative}: {module}"
        for relative, modules in _imports_by_module("hub", inside=True).items()
        for module in sorted(modules)
        if not _matches(module, allowed)
    ]
    assert offenders == []


def test_sync_depends_only_on_storage_and_recording() -> None:
    allowed = (
        "guildbotics.sync",
        "guildbotics.workspace",
        "guildbotics.observability",
        "guildbotics.utils",
        "guildbotics.entities",
    )
    offenders = [
        f"{relative}: {module}"
        for relative, modules in _imports_by_module("sync", inside=True).items()
        for module in sorted(modules)
        if not _matches(module, allowed)
    ]
    assert offenders == []


#: The only modules that may import ``guildbotics.sync``: one per long-lived
#: process, each installing the queue for the workspace that process works in.
SYNC_COMPOSITION_ROOTS = frozenset(
    {
        Path("app_api/workspace_sync.py"),
        Path("cli/__init__.py"),
    }
)


def test_nothing_reaches_around_the_workspace_sync_port() -> None:
    """Capabilities, drivers, integrations, and the API layer announce writes
    through the port; importing the sync package would reintroduce the direct
    dependency on Git the port exists to remove."""
    offenders = [
        f"{relative}: {module}"
        for relative, modules in _imports_by_module("sync", inside=False).items()
        if relative not in SYNC_COMPOSITION_ROOTS
        for module in sorted(modules)
        if _matches(module, ("guildbotics.sync",))
    ]
    assert offenders == []


def test_every_declared_sync_composition_root_installs_the_queue() -> None:
    """A stale entry would quietly widen the exception it declares."""
    imports = _imports_by_module("sync", inside=False)
    unused = [
        str(relative)
        for relative in sorted(SYNC_COMPOSITION_ROOTS)
        if not any(
            _matches(module, ("guildbotics.sync",))
            for module in imports.get(relative, set())
        )
    ]
    assert unused == []


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
