"""No test fakes the platform on the interpreter's own ``sys``.

``module.sys`` is the same object as ``sys``, so setting ``platform`` on it
changes the platform for everything imported while the test runs. A library
imported for the first time in that window takes the branch for a platform it
is not on -- ``mcp.server.stdio`` imports ``fcntl`` on Windows -- and the test
then passes or fails depending on what the same worker happened to import
first. Tests use the ``fake_platform`` fixture instead, which gives only the
module under test a view of ``sys`` with another ``platform``.

The check runs over every test file rather than over the ones that happened to
fail, because the next one written is the one that would bring it back.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent


def _names_sys(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "sys"
    return isinstance(node, ast.Attribute) and node.attr == "sys"


def _fakes_platform(call: ast.Call) -> bool:
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "setattr"):
        return False
    if not call.args:
        return False
    target = call.args[0]
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        return target.value == "sys.platform" or target.value.endswith(".sys.platform")
    return (
        len(call.args) > 1
        and _names_sys(target)
        and isinstance(call.args[1], ast.Constant)
        and call.args[1].value == "platform"
    )


def test_no_test_sets_platform_on_the_shared_sys_module() -> None:
    offenders: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _fakes_platform(node):
                relative = path.relative_to(TESTS_ROOT).as_posix()
                offenders.append(f"{relative}:{node.lineno}")

    assert offenders == [], (
        "These lines set `platform` on the shared `sys` module; use the "
        f"`fake_platform` fixture instead: {offenders}"
    )
