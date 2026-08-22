from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import pytest

from guildbotics.utils import fileio, workspace_sync_port
from guildbotics.workspace.identity import (
    DeviceRecord,
    ensure_device_identity,
    ensure_workspace_identity,
    list_device_records,
    new_uuid7,
    publish_device_record,
    read_device_identity,
    read_workspace_identity,
    set_device_display_name,
    workspace_identity_path,
)
from tests.guildbotics.utils.test_workspace_sync_port import RecordingPort


@pytest.fixture(autouse=True)
def machine_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep the machine-local device identity out of the developer's home."""
    root = tmp_path / "machine"
    monkeypatch.setattr(fileio, "get_machine_state_root", lambda: root)
    return root


def test_new_uuid7_is_a_version_7_uuid() -> None:
    value = uuid.UUID(new_uuid7())

    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_new_uuid7_values_are_unique_and_time_ordered() -> None:
    values = [new_uuid7() for _ in range(50)]

    assert len(set(values)) == len(values)
    # The leading 48 bits are a millisecond timestamp, so values never go backwards.
    timestamps = [uuid.UUID(value).int >> 80 for value in values]
    assert timestamps == sorted(timestamps)


def test_workspace_identity_is_created_once_and_then_reused(
    port: RecordingPort,
) -> None:
    first = ensure_workspace_identity()
    second = ensure_workspace_identity()

    assert first.workspace_id == second.workspace_id
    assert uuid.UUID(first.workspace_id).version == 7
    assert first.schema_version == 1
    assert read_workspace_identity() == first
    assert [change.paths for change in port.changes] == [("state/workspace.json",)]


def test_workspace_identity_is_written_as_a_shared_record(port: RecordingPort) -> None:
    identity = ensure_workspace_identity()

    stored = json.loads(workspace_identity_path().read_text(encoding="utf-8"))
    assert stored == {
        "schema_version": 1,
        "workspace_id": identity.workspace_id,
        "created_at": identity.created_at,
    }


def test_workspace_identity_is_absent_before_first_use() -> None:
    assert read_workspace_identity() is None


def test_device_identity_defaults_to_the_host_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "studio.local")

    identity = ensure_device_identity()

    assert identity.display_name == "studio"
    assert uuid.UUID(identity.device_id).version == 7
    assert ensure_device_identity() == identity
    assert read_device_identity() == identity


def test_the_device_identity_is_never_shared(
    machine_state: Path, port: RecordingPort
) -> None:
    ensure_device_identity()

    assert (machine_state / "device.json").is_file()
    assert port.changes == []


def test_renaming_a_device_keeps_its_identifier() -> None:
    original = ensure_device_identity()

    renamed = set_device_display_name("  Work laptop  ")

    assert renamed.display_name == "Work laptop"
    assert renamed.device_id == original.device_id
    assert read_device_identity() == renamed


def test_renaming_a_device_rejects_a_blank_name() -> None:
    ensure_device_identity()

    with pytest.raises(ValueError):
        set_device_display_name("   ")


def test_publishing_a_device_record_shares_it(port: RecordingPort) -> None:
    identity = ensure_device_identity()

    record = publish_device_record()

    assert record.device_id == identity.device_id
    assert record.display_name == identity.display_name
    assert record.status == "active"
    assert record.ssh_public_key_fingerprint is None
    assert [change.paths for change in port.changes] == [
        (f"state/devices/{identity.device_id}.json",)
    ]


def test_republishing_an_unchanged_device_record_announces_nothing(
    port: RecordingPort,
) -> None:
    publish_device_record()
    port.changes.clear()

    publish_device_record()

    assert port.changes == []


def test_republishing_keeps_the_join_time_and_carries_the_fingerprint(
    port: RecordingPort,
) -> None:
    first = publish_device_record()

    set_device_display_name("Renamed")
    renamed = publish_device_record(ssh_public_key_fingerprint="SHA256:abc")
    kept = publish_device_record()

    assert renamed.joined_at == first.joined_at
    assert renamed.display_name == "Renamed"
    assert renamed.ssh_public_key_fingerprint == "SHA256:abc"
    assert kept.ssh_public_key_fingerprint == "SHA256:abc"


def test_listing_device_records_is_ordered_by_identifier(
    tmp_path: Path, port: RecordingPort
) -> None:
    publish_device_record()
    other = DeviceRecord(
        device_id="00000000-0000-7000-8000-000000000000",
        display_name="Hub",
        os="linux",
        joined_at="2026-08-15T00:00:00Z",
    )
    # Another device's record arrives as a synchronization checkout, not
    # through a writer on this device, so plant the file directly.
    other_path = tmp_path / f".guildbotics/state/devices/{other.device_id}.json"
    other_path.parent.mkdir(parents=True, exist_ok=True)
    other_path.write_text(
        workspace_sync_port.dump_shared_json(other.model_dump()), encoding="utf-8"
    )

    records = list_device_records()

    assert [record.device_id for record in records] == sorted(
        record.device_id for record in records
    )
    assert other in records


def test_concurrent_first_use_agrees_on_one_workspace_id() -> None:
    start = threading.Barrier(4)
    seen: list[str] = []

    def create() -> None:
        start.wait()
        seen.append(ensure_workspace_identity().workspace_id)

    threads = [threading.Thread(target=create) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    stored = read_workspace_identity()
    assert stored is not None
    assert set(seen) == {stored.workspace_id}


def test_concurrent_first_use_agrees_on_one_device_id() -> None:
    start = threading.Barrier(4)
    seen: list[str] = []

    def create() -> None:
        start.wait()
        seen.append(ensure_device_identity().device_id)

    threads = [threading.Thread(target=create) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    stored = read_device_identity()
    assert stored is not None
    assert set(seen) == {stored.device_id}


@pytest.mark.parametrize(
    "override",
    [
        {"schema_version": 999},
        {"device_id": "not-a-uuid"},
        {"joined_at": "yesterday"},
        {"display_name": ""},
        {"os": "plan9"},
    ],
)
def test_a_device_record_pins_version_identifier_and_time(override: dict) -> None:
    payload = {
        "schema_version": 1,
        "device_id": "00000000-0000-7000-8000-000000000000",
        "display_name": "Hub",
        "os": "linux",
        "joined_at": "2026-08-15T00:00:00Z",
    } | override

    with pytest.raises(ValueError):
        DeviceRecord.model_validate(payload)


def test_a_device_record_rejects_device_local_fields() -> None:
    with pytest.raises(ValueError):
        DeviceRecord.model_validate(
            {
                "device_id": "00000000-0000-7000-8000-000000000000",
                "display_name": "Hub",
                "os": "linux",
                "joined_at": "2026-08-15T00:00:00Z",
                "workspace_path": "/Users/me/GuildBotics/main",
            }
        )
