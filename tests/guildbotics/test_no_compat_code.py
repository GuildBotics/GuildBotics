"""Guard the no-backward-compat policy of AGENTS.md (最重要ルール).

The project has effectively one user and README's disclaimer already declares
that breaking changes may happen, so compatibility layers have no
beneficiaries: changes must switch over directly and delete the legacy path.
Compat code almost always announces itself — "legacy" identifiers, "backward
compatibility" comments, migration helpers — so this test trips on those
markers in production source. It is a tripwire, not a semantic judge:
legitimate occurrences (e.g. compatibility with an external tool's protocol
versions) are recorded in ``ALLOWED`` with a reason, and stale allowances fail
the test so the list cannot rot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import guildbotics

PACKAGE_ROOT = Path(guildbotics.__file__).parent
REPOSITORY_ROOT = PACKAGE_ROOT.parent

MARKER_PATTERN = re.compile(
    r"legacy|backwards?[\s_-]*compat|deprecat|migrat", re.IGNORECASE
)


@dataclass(frozen=True)
class Allowance:
    """One approved marker occurrence with the reason it is not compat debt."""

    path: str
    needle: str
    reason: str


_CODEX_PROTOCOL = (
    "Supports both approval-method generations of the external Codex app-server "
    "protocol; compat with an installed external tool, not with our own past."
)

ALLOWED: tuple[Allowance, ...] = (
    Allowance(
        "guildbotics/intelligences/agent_runtime/codex.py",
        "_LEGACY_APPROVAL_METHODS",
        _CODEX_PROTOCOL,
    ),
)


def _production_sources() -> list[Path]:
    sources = [
        path
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        if path.name != "_version.py"
    ]
    desktop_src = REPOSITORY_ROOT / "desktop" / "src"
    sources.extend(
        path
        for path in sorted(desktop_src.rglob("*"))
        if path.suffix in {".ts", ".tsx"} and ".test." not in path.name
    )
    return sources


def test_production_code_has_no_unapproved_compat_markers() -> None:
    unused_allowances = set(ALLOWED)
    offenders: list[str] = []
    for path in _production_sources():
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not MARKER_PATTERN.search(line):
                continue
            allowance = next(
                (
                    entry
                    for entry in ALLOWED
                    if entry.path == relative and entry.needle in line
                ),
                None,
            )
            if allowance is None:
                offenders.append(f"{relative}:{lineno}: {line.strip()}")
            else:
                unused_allowances.discard(allowance)
    assert offenders == [], (
        "Backward-compat markers found in production code. Per AGENTS.md, prefer "
        "a direct switchover and delete the legacy path; if an occurrence is "
        "genuinely not compat debt, add an Allowance with the reason:\n"
        + "\n".join(offenders)
    )
    stale = sorted(f"{entry.path}: {entry.needle}" for entry in unused_allowances)
    assert stale == [], "Stale allowances no longer match any line:\n" + "\n".join(
        stale
    )
