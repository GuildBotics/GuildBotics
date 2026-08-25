from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from guildbotics.hub import host, relay

WORKSPACE_ID = "0198ab00-0000-7000-8000-000000000001"


def test_service_owner_is_claimed_once_and_can_be_transferred(
    machine_root: Path,
) -> None:
    host.create_hub()
    host.create_workspace_repository(WORKSPACE_ID)
    first_device = str(uuid4())
    second_device = str(uuid4())

    owner, claimed = relay.claim_service_owner(WORKSPACE_ID, first_device)
    same_owner, claimed_again = relay.claim_service_owner(WORKSPACE_ID, second_device)

    assert claimed is True
    assert claimed_again is False
    assert owner.workspace_id == WORKSPACE_ID
    assert same_owner.owner_device_id == first_device
    assert relay.read_service_owner(WORKSPACE_ID) == owner

    transferred = relay.transfer_service_owner(WORKSPACE_ID, second_device)
    assert transferred.owner_device_id == second_device
    assert relay.read_service_owner(WORKSPACE_ID) == transferred


def test_live_poll_forwards_opaque_lines_and_expires_old_files(
    machine_root: Path,
) -> None:
    host.create_hub()
    host.create_workspace_repository(WORKSPACE_ID)
    device_id = str(uuid4())
    publisher_id = str(uuid4())
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    current = json.dumps({"observed_at": now.isoformat(), "anything": "kept"})

    path = relay.publish_live_line(WORKSPACE_ID, device_id, publisher_id, current)
    first = relay.poll_live(WORKSPACE_ID, now=now)
    second = relay.poll_live(
        WORKSPACE_ID,
        live_cursor=first.live_cursor,
        head_cursor=first.head_cursor,
        now=now,
    )

    assert first.lines == (current,)
    assert second.lines == ()
    assert path.is_file()

    expired = json.dumps({"observed_at": (now - timedelta(seconds=61)).isoformat()})
    relay.publish_live_line(WORKSPACE_ID, device_id, publisher_id, expired)
    expired_poll = relay.poll_live(WORKSPACE_ID, now=now)

    assert json.loads(expired_poll.lines[0]) == {
        "device_id": device_id,
        "kind": relay.LIVE_EXPIRED_EVENT_KIND,
        "observed_at": (now - timedelta(seconds=61)).isoformat(),
        "publisher_id": publisher_id,
        "schema_version": 1,
        "workspace_id": WORKSPACE_ID,
    }
    assert not path.exists()


def test_head_marker_is_a_watch_event(machine_root: Path) -> None:
    host.create_hub()
    host.create_workspace_repository(WORKSPACE_ID)
    marker = relay.head_updated_path(WORKSPACE_ID)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    result = relay.poll_live(WORKSPACE_ID)

    assert result.lines == (relay.HEAD_UPDATED_EVENT,)


@pytest.mark.parametrize(
    "device_id", ["not-a-uuid", "0198AB00-0000-7000-8000-000000000001"]
)
def test_owner_rejects_noncanonical_device_ids(
    machine_root: Path, device_id: str
) -> None:
    host.create_hub()
    host.create_workspace_repository(WORKSPACE_ID)

    with pytest.raises(relay.HubRelayError):
        relay.claim_service_owner(WORKSPACE_ID, device_id)
