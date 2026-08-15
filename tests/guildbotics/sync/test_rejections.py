"""What a rejection tells the user, and what it deliberately does not."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from guildbotics.observability.activity_event_store import (
    ActivityEventStore,
    is_domain_activity_event,
)
from guildbotics.observability.event_types import SYNC_UPDATE_REJECTED
from guildbotics.sync.rejections import record_update_rejected
from guildbotics.utils import workspace_sync_port
from guildbotics.workspace.validation import validate_shared_file
from tests.guildbotics.sync.conftest import WORKSPACE_ID, Device
from tests.guildbotics.utils.test_workspace_sync_port import RecordingPort


@pytest.fixture
def recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(workspace_sync_port, "_port", RecordingPort())
    record_update_rejected(
        rejection_id="0198ab00-0000-7000-8000-0000000000aa",
        paths=["config/team/project.yml", "state/chat_state/slack/aiko/c.json"],
        device_id="device-mac",
        workspace_id=WORKSPACE_ID,
        workspace_root=tmp_path,
    )
    root = tmp_path / ".guildbotics" / "state" / "events"
    written = sorted(root.glob("*/*/*.json"))
    assert len(written) == 1
    return json.loads(written[0].read_text(encoding="utf-8"))


def test_a_rejection_says_where_the_stashed_commit_can_be_found(recorded: dict) -> None:
    assert recorded["kind"] == SYNC_UPDATE_REJECTED
    assert recorded["device_id"] == "device-mac"
    assert recorded["workspace_id"] == WORKSPACE_ID
    assert recorded["payload"]["rejection_id"] == "0198ab00-0000-7000-8000-0000000000aa"
    assert recorded["payload"]["source_device_id"] == "device-mac"
    assert recorded["payload"]["paths"] == [
        "config/team/project.yml",
        "state/chat_state/slack/aiko/c.json",
    ]
    assert recorded["occurred_at"]


def test_a_rejection_never_carries_the_content_that_was_not_accepted(
    recorded: dict,
) -> None:
    """The stashed content is recoverable only on the source device, by hand.
    Putting it here would publish it to every device and to every API."""
    assert set(recorded["payload"]) == {"rejection_id", "paths", "source_device_id"}
    assert recorded["links"] == []


def test_a_rejection_is_shared_activity_and_validates_as_one(recorded: dict) -> None:
    assert is_domain_activity_event(SYNC_UPDATE_REJECTED)
    validate_shared_file(
        f"state/events/2026/08/{recorded['event_id']}.json",
        json.dumps(recorded).encode("utf-8"),
    )


def test_a_real_rejection_is_shared_with_the_reconciled_commit(
    first: Device, second: Device, hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The event is written into shared state during convergence, so the other
    devices learn about the rejection through the same synchronization."""
    from guildbotics.sync import rejections

    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(second.root))
    monkeypatch.setattr(
        second.manager, "_record_rejection", rejections.record_update_rejected
    )
    first.write("config/team/project.yml", "language: ja\n")
    second.write("config/team/project.yml", "language: en\n")
    first.manager.synchronize()

    second.manager.synchronize()
    first.manager.synchronize()

    events = sorted((first.shared / "state" / "events").glob("*/*/*.json"))
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in events]
    rejected = [item for item in payloads if item["kind"] == SYNC_UPDATE_REJECTED]
    assert len(rejected) == 1
    assert rejected[0]["payload"]["source_device_id"] == "device-windows"
    assert rejected[0]["payload"]["paths"] == ["config/team/project.yml"]
