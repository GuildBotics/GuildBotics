"""Guard against drift between ``uv.lock`` and the microVM's dependency list.

``templates/environment/requirements.txt`` is what the snapshot installs for
GuildBotics' own code inside the microVM. It is exported from ``uv.lock``
for the guest; this test exports it again
and fails when a lock change was not exported. Lines are compared as a set, so
the exporter's ordering is not part of the contract.
"""

from __future__ import annotations

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
