from __future__ import annotations

import pytest

from guildbotics.utils import workspace_sync_port
from tests.guildbotics.utils.test_workspace_sync_port import RecordingPort


@pytest.fixture
def port(monkeypatch: pytest.MonkeyPatch) -> RecordingPort:
    """Capture what the storage layers announce to the sync queue."""
    recording = RecordingPort()
    monkeypatch.setattr(workspace_sync_port, "_port", recording)
    return recording
