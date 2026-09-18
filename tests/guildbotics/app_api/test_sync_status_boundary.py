"""Every production pairing of the running queue with a workspace is one call.

``current_sync_manager()`` returns a pointer. Combining it with a later
ambient workspace read is how GET mixed manager A's status with workspace
B's hub URL. The population is every production call of that getter.

Callers that need both go through ``run_current_sync``, which holds the
activation lock across the pair. New pairing sites have to be named here
so the next mix cannot land in an unnamed function.
"""

from __future__ import annotations

from pathlib import Path

import guildbotics

PRODUCTION_ROOT = Path(guildbotics.__file__).resolve().parent
GETTER = PRODUCTION_ROOT / "sync" / "activation.py"

#: Production modules allowed to call ``run_current_sync``, and why.
SNAPSHOT_CALLERS = {
    GETTER: "defines the lifecycle operation",
    PRODUCTION_ROOT / "app_api" / "workspace_sync.py": (
        "status, retry, service-owner prepare, and relay restart"
    ),
}


def _production_python() -> list[Path]:
    return [path for path in PRODUCTION_ROOT.rglob("*.py") if path.is_file()]


def test_the_queue_pointer_is_not_read_outside_activation() -> None:
    """A status mix starts by reading the pointer, then the selected repo."""
    stray = [
        path.relative_to(PRODUCTION_ROOT).as_posix()
        for path in _production_python()
        if path.resolve() != GETTER.resolve()
        and "current_sync_manager(" in path.read_text(encoding="utf-8")
    ]
    assert stray == []


def test_lifecycle_operation_callers_are_named() -> None:
    """New pairings cannot appear without being counted."""
    allowed = {path.resolve() for path in SNAPSHOT_CALLERS}
    unnamed = [
        path.relative_to(PRODUCTION_ROOT).as_posix()
        for path in _production_python()
        if "run_current_sync(" in path.read_text(encoding="utf-8")
        and path.resolve() not in allowed
    ]
    missing = [
        path.relative_to(PRODUCTION_ROOT).as_posix()
        for path in SNAPSHOT_CALLERS
        if not path.is_file()
    ]
    assert unnamed == []
    assert missing == []
