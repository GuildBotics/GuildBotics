from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import pytest

from guildbotics.utils import fileio
from guildbotics.utils.shared_writes import (
    SHARED_RECORD_SCHEMA_VERSION,
    dump_shared_json,
)
from guildbotics.workspace.identity import (
    DeviceIdentity,
    WorkspaceIdentity,
    ensure_device_identity,
    ensure_workspace_identity,
    new_uuid7,
    read_device_identity,
    read_workspace_identity,
    workspace_identity_path,
)


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


def test_workspace_identity_is_created_once_and_then_reused() -> None:
    first = ensure_workspace_identity()
    second = ensure_workspace_identity()

    assert first.workspace_id == second.workspace_id
    assert uuid.UUID(first.workspace_id).version == 7
    assert first.schema_version == SHARED_RECORD_SCHEMA_VERSION
    assert read_workspace_identity() == first


def test_workspace_identity_is_written_as_a_record() -> None:
    identity = ensure_workspace_identity()

    stored = json.loads(workspace_identity_path().read_text(encoding="utf-8"))
    assert stored == {
        "schema_version": SHARED_RECORD_SCHEMA_VERSION,
        "workspace_id": identity.workspace_id,
        "created_at": identity.created_at,
    }


def test_workspace_identity_is_absent_before_first_use() -> None:
    assert read_workspace_identity() is None


def test_v1_identity_files_are_reused_without_changing_identifiers(
    machine_state: Path,
) -> None:
    workspace = {
        "schema_version": 1,
        "workspace_id": "00000000-0000-7000-8000-000000000001",
        "created_at": "2026-08-15T00:00:00Z",
    }
    device = {
        "schema_version": 1,
        "device_id": "00000000-0000-7000-8000-000000000002",
        "display_name": "Existing laptop",
        "os": "linux",
    }
    paths = [workspace_identity_path(), machine_state / "device.json"]
    for path, payload in zip(paths, [workspace, device], strict=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_shared_json(payload), encoding="utf-8")

    assert read_workspace_identity().model_dump() == workspace
    assert ensure_workspace_identity().model_dump() == workspace
    assert read_device_identity().model_dump() == device
    assert ensure_device_identity().model_dump() == device


@pytest.mark.parametrize("model", [WorkspaceIdentity, DeviceIdentity])
@pytest.mark.parametrize(
    "version, accepted",
    [
        (1, True),
        (SHARED_RECORD_SCHEMA_VERSION, True),
        (0, False),
        (SHARED_RECORD_SCHEMA_VERSION + 1, False),
    ],
)
def test_all_identity_records_accept_supported_versions_only(
    model, version, accepted
) -> None:
    payload = (
        {
            "workspace_id": "00000000-0000-7000-8000-000000000001",
            "created_at": "2026-08-15T00:00:00Z",
        }
        if model is WorkspaceIdentity
        else {
            "device_id": "00000000-0000-7000-8000-000000000002",
            "display_name": "Existing laptop",
            "os": "linux",
        }
    )
    payload["schema_version"] = version
    if accepted:
        assert model.model_validate(payload).schema_version == version
    else:
        with pytest.raises(ValueError):
            model.model_validate(payload)


def test_device_identity_defaults_to_the_host_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "studio.local")

    identity = ensure_device_identity()

    assert identity.display_name == "studio"
    assert uuid.UUID(identity.device_id).version == 7
    assert ensure_device_identity() == identity
    assert read_device_identity() == identity


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


def test_a_device_identity_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        DeviceIdentity.model_validate(
            {
                "device_id": "00000000-0000-7000-8000-000000000000",
                "display_name": "Hub",
                "os": "linux",
                "workspace_path": "/Users/me/GuildBotics/main",
            }
        )
