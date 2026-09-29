"""Device probe ownership must not depend on other environments staying idle."""

from pathlib import Path

import pytest

from tests.guildbotics.intelligences.agent_environment import test_credential_boundary


def test_probe_tracks_its_vms_while_unrelated_vms_come_and_go(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = test_credential_boundary
    monkeypatch.setattr(probe, "_SANDBOXES", tmp_path)
    monkeypatch.setattr(probe.runtime, "_NAME_PREFIX", "test-owned-")
    existing = tmp_path / "guildbotics-existing"
    existing.mkdir()
    before = probe._sandboxes()
    owned = tmp_path / "test-owned-vm"
    owned.mkdir()
    (tmp_path / "guildbotics-concurrent").mkdir()
    existing.rmdir()

    assert probe._sandboxes() - before == {owned.name}
    owned.rmdir()
    assert probe._sandboxes() == before
