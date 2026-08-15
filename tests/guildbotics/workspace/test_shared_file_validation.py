from __future__ import annotations

import json

import pytest
import yaml  # type: ignore

from guildbotics.workspace.validation import (
    MAX_SHARED_AVATAR_BYTES,
    MAX_SHARED_FILE_BYTES,
    MAX_SHARED_JOURNAL_BYTES,
    SharedFileInvalidError,
    validate_shared_file,
)

PERSON_YAML = yaml.safe_dump({"person_id": "yuki", "name": "Yuki Nakamura"}).encode()
PROJECT_YAML = yaml.safe_dump({"name": "GuildBotics"}).encode()
WORKSPACE_JSON = json.dumps(
    {
        "schema_version": 1,
        "workspace_id": "019c5e8d-31ce-7a62-a8a9-6ce16cb88945",
        "created_at": "2026-08-15T00:00:00Z",
    }
).encode()
DEVICE_JSON = json.dumps(
    {
        "schema_version": 1,
        "device_id": "019c5e8d-31ce-7a62-a8a9-6ce16cb88945",
        "display_name": "Hub",
        "os": "linux",
        "joined_at": "2026-08-15T00:00:00Z",
        "status": "active",
    }
).encode()


@pytest.mark.parametrize(
    ("relative_path", "data"),
    [
        ("config/team/project.yml", PROJECT_YAML),
        ("config/team/members/yuki/person.yml", PERSON_YAML),
        ("config/hotkeys.yml", yaml.safe_dump({"hotkeys": []}).encode()),
        ("config/commands/build.md", "# Build\n".encode()),
        ("state/workspace.json", WORKSPACE_JSON),
        (
            "state/devices/019c5e8d-31ce-7a62-a8a9-6ce16cb88945.json",
            DEVICE_JSON,
        ),
        ("state/events/2026/08/e1.json", b'{"event_id": "e1"}'),
        ("state/documents/team/abc/body.md", "note\n".encode()),
        ("state/documents/memory_events.jsonl", b'{"kind": "memory"}\n\n'),
        ("config/team/members/yuki/avatar.png", b"\x89PNG\r\n\x1a\n"),
    ],
)
def test_valid_shared_files_pass(relative_path: str, data: bytes) -> None:
    validate_shared_file(relative_path, data)


def test_a_path_outside_the_shared_roots_is_rejected() -> None:
    with pytest.raises(SharedFileInvalidError) as error:
        validate_shared_file("local/run/service.lock", b"")

    assert "outside the shared" in error.value.reason


@pytest.mark.parametrize(
    ("relative_path", "data", "fragment"),
    [
        (
            "config/team/members/yuki/person.yml",
            yaml.safe_dump({"name": "no id"}).encode(),
            "does not match Person",
        ),
        (
            "state/workspace.json",
            json.dumps({"schema_version": 1}).encode(),
            "does not match WorkspaceIdentity",
        ),
        (
            "state/devices/one.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "device_id": "one",
                    "display_name": "Hub",
                    "os": "plan9",
                    "joined_at": "2026-08-15T00:00:00Z",
                }
            ).encode(),
            "does not match DeviceRecord",
        ),
        ("config/team/project.yml", b"name: [unclosed", "is not valid YAML"),
        ("state/chat_state/pending/e1.json", b"{not json}", "is not valid JSON"),
        ("state/documents/memory_events.jsonl", b"{}\nnope\n", "line 2"),
        ("state/documents/team/abc/body.md", b"\xff\xfe", "is not valid UTF-8"),
    ],
)
def test_invalid_shared_files_are_rejected(
    relative_path: str, data: bytes, fragment: str
) -> None:
    with pytest.raises(SharedFileInvalidError) as error:
        validate_shared_file(relative_path, data)

    assert fragment in error.value.reason
    assert error.value.relative_path == relative_path


def test_a_shared_record_rejects_device_local_fields() -> None:
    payload = json.loads(DEVICE_JSON) | {"pid": 4321, "cache_dir": "/tmp/cache"}

    with pytest.raises(SharedFileInvalidError):
        validate_shared_file("state/devices/one.json", json.dumps(payload).encode())


def test_an_oversized_record_is_rejected() -> None:
    data = b'{"note": "' + b"x" * MAX_SHARED_FILE_BYTES + b'"}'

    with pytest.raises(SharedFileInvalidError) as error:
        validate_shared_file("state/chat_state/pending/e1.json", data)

    assert "above the" in error.value.reason


def test_a_journal_may_exceed_the_record_limit_but_not_its_own() -> None:
    line = b'{"kind": "memory"}\n'
    within = line * (MAX_SHARED_FILE_BYTES // len(line) + 1)
    assert len(within) > MAX_SHARED_FILE_BYTES

    validate_shared_file("state/documents/memory_events.jsonl", within)

    beyond = line * (MAX_SHARED_JOURNAL_BYTES // len(line) + 1)
    with pytest.raises(SharedFileInvalidError):
        validate_shared_file("state/documents/memory_events.jsonl", beyond)


def test_an_avatar_must_be_a_supported_image_kind() -> None:
    with pytest.raises(SharedFileInvalidError) as error:
        validate_shared_file("config/team/members/yuki/avatar.svg", b"<svg/>")

    assert "supported avatar image" in error.value.reason


def test_an_avatar_may_be_binary_up_to_its_own_limit() -> None:
    validate_shared_file(
        "config/team/members/yuki/avatar.png", b"\xff" * MAX_SHARED_AVATAR_BYTES
    )

    with pytest.raises(SharedFileInvalidError):
        validate_shared_file(
            "config/team/members/yuki/avatar.png",
            b"\xff" * (MAX_SHARED_AVATAR_BYTES + 1),
        )
