"""Stable, readable filesystem components for untrusted logical identifiers."""

from __future__ import annotations

import hashlib


def safe_path_component(value: str) -> str:
    """Return a bounded readable component with a collision-resistant suffix."""
    readable = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"{readable[:48]}-{digest}"
