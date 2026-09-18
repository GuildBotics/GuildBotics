"""Every production pairing of the running queue with a workspace is one call.

``current_sync_manager()`` returns a pointer. Combining it with a later
ambient workspace read is how GET mixed manager A's status with workspace
B's hub URL. The population is every production call of that getter.

Callers that need both go through ``run_current_sync``, which holds the
activation lock across the pair. New pairing sites have to be named here
so the next mix cannot land in an unnamed function.
"""

from __future__ import annotations

import ast
from pathlib import Path

import guildbotics

PRODUCTION_ROOT = Path(guildbotics.__file__).resolve().parent
REPOSITORY_ROOT = PRODUCTION_ROOT.parent

#: ``(module path, enclosing function)`` for every lifecycle operation.
CURRENT_SYNC_OPERATIONS = {
    ("guildbotics/app_api/workspace_sync.py", "WorkspaceSyncService.get_status"),
    (
        "guildbotics/app_api/workspace_sync.py",
        "WorkspaceSyncService.prepare_service_owner",
    ),
    ("guildbotics/app_api/workspace_sync.py", "WorkspaceSyncService.retry"),
    ("guildbotics/app_api/workspace_sync.py", "WorkspaceSyncService._paused"),
}


def _call_names(tree: ast.AST, symbol: str) -> set[str]:
    """Return names that directly import one callable, including aliases."""
    names = {symbol}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        names.update(
            alias.asname or alias.name for alias in node.names if alias.name == symbol
        )
    return names


def _enclosing_function(node: ast.AST) -> str:
    names: list[str] = []
    current: ast.AST | None = getattr(node, "parent", None)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(current.name)
        current = getattr(current, "parent", None)
    return ".".join(reversed(names)) or "<module>"


def _production_call_sites(symbol: str) -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for path in sorted(PRODUCTION_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        direct_names = _call_names(tree, symbol)
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child.parent = parent  # type: ignore[attr-defined]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            is_direct = isinstance(called, ast.Name) and called.id in direct_names
            is_qualified = isinstance(called, ast.Attribute) and called.attr == symbol
            if not is_direct and not is_qualified:
                continue
            module = path.relative_to(REPOSITORY_ROOT).as_posix()
            sites.add((module, _enclosing_function(node)))
    return sites


def test_the_queue_pointer_is_not_read_outside_activation() -> None:
    """A status mix starts by reading the pointer, then the selected repo."""
    assert _production_call_sites("current_sync_manager") == set()


def test_lifecycle_operation_callers_are_named() -> None:
    """New pairings cannot appear without being counted."""
    assert _production_call_sites("run_current_sync") == CURRENT_SYNC_OPERATIONS
