"""Rejected local changes as the activity timeline reports them.

A rejection is not a failure to fix, so it never becomes an error state. What
the timeline owes the user is only enough to find the stashed commit again on
the device that made it: the paths, that device, the time, and the identifier.
The stashed content itself stays out of the API.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from guildbotics.app_api.activity_history import build_activity_history
from guildbotics.entities.team import Person
from guildbotics.observability.event_types import SYNC_UPDATE_REJECTED

START = datetime(2026, 7, 1, tzinfo=UTC)
END = datetime(2026, 7, 2, tzinfo=UTC)


def _record(**overrides: Any) -> dict[str, Any]:
    record = {
        "type": SYNC_UPDATE_REJECTED,
        "timestamp": "2026-07-01T11:00:00+00:00",
        "workspace_id": "1f0a0000-0000-7000-8000-000000000001",
        "device_id": "1f0a0000-0000-7000-8000-0000000000d1",
        "subject": "r-1",
        "payload": {
            "rejection_id": "r-1",
            "paths": ["config/team/project.yml", "state/devices/d1.json"],
            "source_device_id": "1f0a0000-0000-7000-8000-0000000000d1",
        },
    }
    record.update(overrides)
    return record


def _events(*records: dict[str, Any]):
    return build_activity_history(
        start=START,
        end=END,
        members=[Person(person_id="alice", name="Alice", person_type="agent")],
        records=list(records),
    ).events


def test_a_rejected_update_reaches_the_timeline() -> None:
    events = _events(_record())

    assert [event.type for event in events] == ["sync_rejected"]


def test_the_facts_needed_to_find_the_stashed_commit_are_reported() -> None:
    event = _events(_record())[0]

    assert event.rejection is not None
    assert event.rejection.rejection_id == "r-1"
    assert event.rejection.paths == [
        "config/team/project.yml",
        "state/devices/d1.json",
    ]
    assert event.rejection.source_device_id == "1f0a0000-0000-7000-8000-0000000000d1"
    assert event.timestamp == "2026-07-01T11:00:00+00:00"


def test_a_rejection_names_the_paths_and_links_to_nothing() -> None:
    """Recovery is a manual procedure on one device, so there is nowhere to go."""
    event = _events(_record())[0]

    assert event.detail == "config/team/project.yml, state/devices/d1.json"
    assert event.url == ""
    assert event.links == []


def test_no_stashed_content_is_carried() -> None:
    event = _events(_record())[0]

    assert set(event.rejection.model_dump()) == {
        "rejection_id",
        "paths",
        "source_device_id",
    }


def test_a_record_without_an_identifier_is_not_shown() -> None:
    """Without it the user would be told something happened but not where to look."""
    events = _events(
        _record(payload={"paths": ["config/team/project.yml"], "source_device_id": "d"})
    )

    assert events == []


def test_a_rejection_belongs_to_no_member() -> None:
    """Synchronization is the device's work, not any member's."""
    event = _events(_record())[0]

    assert event.person_id == ""
