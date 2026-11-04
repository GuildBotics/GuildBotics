"""Guard against drift between ``uv.lock`` and the microVM's dependency list.

``templates/environment/requirements.txt`` is what the snapshot installs for
GuildBotics' own code inside the microVM. It is exported from ``uv.lock``
for the guest; this test exports it again
and fails when a lock change was not exported. Lines are compared as a set, so
the exporter's ordering is not part of the contract.
"""

from __future__ import annotations

import ast
import importlib.metadata
import re
import sys
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "guildbotics/templates/environment/requirements.txt"
EXPORT = [
    "uv",
    "export",
    "--frozen",
    "--no-dev",
    "--no-emit-project",
    "--no-header",
    "--no-annotate",
    "--no-hashes",
]


def _lines(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines() if line.strip()}


def test_the_microvm_dependencies_match_the_lock() -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to export the dependency list."
    exported = subprocess.run(
        [uv, *EXPORT[1:]],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    committed = _lines(REQUIREMENTS.read_text(encoding="utf-8"))
    assert committed == _lines(exported), (
        f"{REQUIREMENTS} is out of date for uv.lock. Regenerate it with: "
        f"{' '.join(EXPORT)} -o {REQUIREMENTS.relative_to(REPO_ROOT).as_posix()}"
    )
    assert not any(
        line.startswith(("microsandbox==", "wakepy==")) for line in committed
    )


def _distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_every_static_third_party_import_is_in_guest_requirements() -> None:
    """Check source imports without importing modules or trusting stale installs.

    Distribution metadata translates import names such as yaml/PyYAML. The
    exported runtime list, rather than dev dependencies or installed extras,
    decides whether an imported distribution belongs to the guest.
    """
    runtime = {
        _distribution_name(line.split("==", 1)[0])
        for line in _lines(REQUIREMENTS.read_text(encoding="utf-8"))
        if not line.startswith("#")
    }
    distributions = importlib.metadata.packages_distributions()
    missing = []
    for path in (REPO_ROOT / "guildbotics").rglob("*.py"):
        if path.name == "_version.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                modules = [node.module]
            for module in modules:
                root = module.split(".", 1)[0]
                if root in sys.stdlib_module_names or root == "guildbotics":
                    continue
                owners = {
                    _distribution_name(name) for name in distributions.get(root, [])
                }
                if not owners & runtime:
                    missing.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {module}"
                    )
    assert missing == [], (
        "Third-party imports missing from guest requirements: " + ", ".join(missing)
    )
