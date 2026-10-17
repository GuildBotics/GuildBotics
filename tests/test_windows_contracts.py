"""Windows-only tests must remain selected when CI runs the OS contracts.

This checks direct platform exclusions, including inherited pytest marks.
Portable tests with Windows branches still need review against their callers;
the syntax check cannot decide which of those contracts belong on Windows.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent


def _mark_name(mark: ast.expr) -> str:
    return ast.unparse(mark.func if isinstance(mark, ast.Call) else mark)


def _windows_only(mark: ast.expr) -> bool:
    if not isinstance(mark, ast.Call) or _mark_name(mark) != "pytest.mark.skipif":
        return False
    condition = next(
        iter(mark.args),
        next((kw.value for kw in mark.keywords if kw.arg == "condition"), None),
    )
    return (
        isinstance(condition, ast.Compare)
        and len(condition.ops) == 1
        and isinstance(condition.ops[0], ast.NotEq)
        and isinstance(condition.comparators[0], ast.Constant)
        and (ast.unparse(condition.left), condition.comparators[0].value)
        in {("os.name", "nt"), ("sys.platform", "win32")}
    )


def _missing_windows_markers(source: str) -> list[str]:
    missing: list[str] = []

    def visit(body: list[ast.stmt], inherited: tuple[ast.expr, ...]) -> None:
        marks = list(inherited)
        for node in body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "pytestmark"
                for target in node.targets
            ):
                value = node.value
                marks.extend(
                    value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
                )
        for node in body:
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            effective = (*marks, *node.decorator_list)
            if isinstance(node, ast.ClassDef):
                visit(node.body, effective)
            elif (
                node.name.startswith("test_")
                and any(_windows_only(mark) for mark in effective)
                and not any(
                    _mark_name(mark) == "pytest.mark.windows" for mark in effective
                )
            ):
                missing.append(f"{node.name}:{node.lineno}")

    visit(ast.parse(source).body, ())
    return missing


def test_windows_only_tests_have_the_windows_marker() -> None:
    missing = [
        f"{path.relative_to(TESTS_ROOT).as_posix()}::{test}"
        for path in sorted(TESTS_ROOT.rglob("*.py"))
        for test in _missing_windows_markers(path.read_text(encoding="utf-8"))
    ]
    assert not missing, f"Windows-only tests need @pytest.mark.windows: {missing}"


@pytest.mark.parametrize("platform", ['os.name != "nt"', 'sys.platform != "win32"'])
@pytest.mark.parametrize("scope", ["function", "class", "module"])
@pytest.mark.parametrize("marked", [False, True])
def test_windows_only_markers_are_checked_in_every_scope(platform, scope, marked):
    skip = f"pytest.mark.skipif({platform})"
    windows = ["pytest.mark.windows"] if marked else []
    marks = [skip, *windows]
    if scope == "module":
        source = f"pytestmark = [{', '.join(marks)}]\ndef test_example(): pass\n"
    elif scope == "class":
        source = f"class TestExample:\n    pytestmark = [{', '.join(marks)}]\n    async def test_example(self): pass\n"
    else:
        source = "".join(f"@{mark}\n" for mark in marks) + "def test_example(): pass\n"

    assert bool(_missing_windows_markers(source)) is not marked


def test_other_platform_and_non_platform_skips_need_no_windows_marker():
    source = """
@pytest.mark.skipif(os.name == "nt")
def test_posix(): pass
@pytest.mark.skipif(sys.platform != "darwin")
def test_macos(): pass
@pytest.mark.skipif(not can_bind_localhost())
def test_network(): pass
"""
    assert _missing_windows_markers(source) == []
