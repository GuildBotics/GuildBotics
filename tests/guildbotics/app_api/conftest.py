from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_machine_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep App API service locks out of the developer's real home directory."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
