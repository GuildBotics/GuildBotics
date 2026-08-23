from __future__ import annotations

import json

import pytest
import yaml  # type: ignore

from guildbotics.workspace.validation import (
    MAX_SHARED_AVATAR_BYTES,
    MAX_SHARED_FILE_BYTES,
    MAX_SHARED_JOURNAL_BYTES,
    SharedFileInvalidError,
    SharedSchemaAheadError,
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
ACTIVITY_EVENT_JSON = json.dumps(
    {
        "schema_version": 1,
        "event_id": "e1",
        "occurred_at": "2026-08-15T00:00:00Z",
        "kind": "github.push",
    }
).encode()
SECRETS_INDEX = yaml.safe_dump(
    {"store_id": "abc", "keys": {"GITHUB_TOKEN": {"generation": 2}}}
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
        ("state/events/2026/08/e1.json", ACTIVITY_EVENT_JSON),
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
    ("relative_path", "data"),
    [
        ("config/team/project.yml", yaml.safe_dump({"unknown": True}).encode()),
        (
            "config/team/members/yuki/person.yml",
            yaml.safe_dump({"name": "no id"}).encode(),
        ),
        ("config/intelligences/model_mapping.yml", b"default: models/openai.yml\n"),
        ("config/commands/build.md", b"---\nname: [unclosed\n---\n"),
        ("config/commands/build.py", b"def main(  # unfinished\n"),
    ],
)
def test_files_a_person_authors_travel_as_written(
    relative_path: str, data: bytes
) -> None:
    """The boundary is a damage detector, not a quality gate. A half-finished
    edit fails loudly through the product's own paths on every device, and
    refusing to carry it would only stop the user continuing on another
    machine."""
    validate_shared_file(relative_path, data)


@pytest.mark.parametrize(
    ("relative_path", "data", "fragment"),
    [
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


@pytest.mark.parametrize(
    "relative_path",
    [
        "state/workspace.json",
        "state/events/2026/08/e1.json",
        "state/documents/team/abc/meta.yml",
    ],
)
def test_a_record_from_a_newer_build_stops_the_boundary(relative_path: str) -> None:
    """The one thing no writer and no local test can catch: a device running an
    older build cannot read a record a newer one wrote."""
    payload = json.dumps({"schema_version": 2, "anything": "else"}).encode()
    if relative_path.endswith(".yml"):
        payload = yaml.safe_dump({"schema_version": 2}).encode()

    with pytest.raises(SharedSchemaAheadError) as error:
        validate_shared_file(relative_path, payload)

    assert error.value.version == 2


def test_a_newer_task_run_record_stops_the_boundary() -> None:
    with pytest.raises(SharedSchemaAheadError):
        validate_shared_file(
            "state/task-runs/run-1/result.json",
            b'{"schema_version": 2}\n',
        )


def test_the_current_schema_version_is_carried_normally() -> None:
    validate_shared_file("state/events/2026/08/e1.json", ACTIVITY_EVENT_JSON)


def test_the_secret_index_may_name_keys_but_hold_no_value() -> None:
    validate_shared_file("config/secrets.yml", SECRETS_INDEX)


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"store_id": "a", "keys": {"GITHUB_TOKEN": "ghp-x"}}, "stores a value"),
        (
            {
                "store_id": "a",
                "keys": {"GITHUB_TOKEN": {"generation": 1, "value": "x"}},
            },
            "records value",
        ),
        ({"store_id": "a", "keys": {}, "values": {}}, "carries values"),
        ({"store_id": "a", "keys": {"T": {"generation": "one"}}}, "non-numeric"),
    ],
)
def test_the_secret_index_refuses_anywhere_a_value_could_sit(
    payload: dict, fragment: str
) -> None:
    """Secrets stay out of the shared history because the index has nowhere to
    put one, not because values are recognized and stripped."""
    with pytest.raises(SharedFileInvalidError) as error:
        validate_shared_file("config/secrets.yml", yaml.safe_dump(payload).encode())

    assert fragment in error.value.reason


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
