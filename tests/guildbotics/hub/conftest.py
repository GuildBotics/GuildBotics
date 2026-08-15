"""A hub on a machine of its own, so no test can touch the developer's home."""

from __future__ import annotations

from pathlib import Path

import pytest

from guildbotics.hub import host
from guildbotics.utils import fileio


@pytest.fixture
def machine_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect ``~/.guildbotics`` to a temporary directory."""
    root = tmp_path / "machine" / ".guildbotics"
    monkeypatch.setattr(fileio, "get_machine_root", lambda: root)
    monkeypatch.setattr(host, "get_machine_root", lambda: root)
    return root
