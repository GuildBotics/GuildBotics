"""Shared brain-selection semantics for command execution and validation."""

from __future__ import annotations

_DISABLED_BRAINS = frozenset({"none", "-", "null", "disabled"})


def is_brain_disabled(value: object) -> bool:
    """Return whether a command explicitly disables brain execution."""
    return str(value).strip().lower() in _DISABLED_BRAINS
