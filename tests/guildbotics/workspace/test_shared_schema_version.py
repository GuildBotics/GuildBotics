"""Every record GuildBotics writes under ``state/`` declares its generation.

``schema_version`` is the one thing the receiving device can check for itself.
A record written by a newer build reaches an older one intact and syntactically
fine; what the older build cannot do is read it, and neither the writer nor any
test on the writing machine is in a position to notice. The commit boundary
rejects a version it does not implement -- but only for records that carry one,
and the readers here default quietly around missing fields, so a record without
a version is read as though it were understood.

The population is therefore every shared record, and it is taken from a
workspace rather than from a list: the writers below are run for real, and then
everything they left under ``state/`` has to account for itself. A new record
kind that an existing operation starts writing is caught without anyone
remembering this file exists.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
import yaml  # type: ignore

from guildbotics.capabilities.member_memory import MemberMemoryService
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.chat_state_store import (
    ChannelCursorState,
    ScheduledPostState,
    ThreadConversationState,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.utils.workspace_sync_port import SHARED_RECORD_SCHEMA_VERSION
from guildbotics.workspace.identity import (
    ensure_workspace_identity,
    publish_device_record,
)
from guildbotics.workspace.validation import (
    SharedSchemaAheadError,
    validate_shared_file,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "guildbotics"

#: File kinds under ``state/`` that hold no record, and so have nowhere to put
#: a generation. Classifying by kind rather than by path is what lets the check
#: below run on whatever the writers actually produce, including files whose
#: names are generated.
WITHOUT_A_GENERATION = {
    ".md": "a document's prose, written by the member",
    ".txt": "a list of document ids, one per line",
}

#: File kinds that carry one record, or one per line, and must declare it.
RECORD_SUFFIXES = {".json", ".jsonl", ".yml", ".yaml"}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    return tmp_path


def _write_one_of_every_shared_record(workspace: Path) -> None:
    """Run the real writers, so the population comes from what they produce."""
    ensure_workspace_identity()
    publish_device_record()

    ActivityEventStore().record(
        {
            "type": "github.pull_request.opened",
            "person_id": "p1",
            "safe_summary": "opened a pull request",
        }
    )

    store = FileConversationStateStore()
    store.save_channel_cursor("slack", "p1", "C1", ChannelCursorState(cursor="c"))
    store.save_thread_state(
        "slack",
        "p1",
        "C1",
        "1.0",
        ThreadConversationState(channel_id="C1", thread_ts="1.0"),
    )
    store.save_scheduled_post_state(
        "slack", "p1", "daily", ScheduledPostState(last_run_slot="s")
    )
    store.upsert_pending_event(
        "slack",
        "p1",
        "C1",
        ChatEvent(
            event_id="e1",
            channel_id="C1",
            message_ts="1.0",
            thread_ts="1.0",
            author_id="U1",
            text="hello",
        ),
    )
    store.save_receive_cutoff("slack", "p1", "1.0")

    # Writes the document metadata, the recency list, and an audit event.
    MemberMemoryService(Person(person_id="p1", name="P")).record(
        scope="personal", title="seed", body="body"
    )

    RunStore().append("run-1", {"kind": "evidence", "evidence_type": "commit"})


def _shared_files(workspace: Path) -> dict[str, Path]:
    state = workspace / ".guildbotics" / "state"
    return {
        path.relative_to(workspace / ".guildbotics").as_posix(): path
        for path in sorted(state.rglob("*"))
        if path.is_file()
    }


def test_every_record_written_under_state_declares_its_generation(
    workspace: Path,
) -> None:
    """Checked on what the writers produce, not on a list of record kinds.

    The kinds were added one at a time, and the three that carried a version
    were the three someone thought about. Reading them back off disk is what
    makes the next one impossible to forget.
    """
    _write_one_of_every_shared_record(workspace)
    written = _shared_files(workspace)
    assert written, "the writers produced nothing to check"

    unclassified = sorted(
        {
            relative
            for relative, path in written.items()
            if path.suffix not in RECORD_SUFFIXES
            and path.suffix not in WITHOUT_A_GENERATION
        }
    )
    assert not unclassified, (
        "These are neither a record kind nor excused from carrying a "
        f"generation: {unclassified}"
    )

    missing: list[str] = []
    for relative, path in written.items():
        if path.suffix not in RECORD_SUFFIXES:
            continue
        records = _records_in(path)
        assert records, f"{relative} is a record kind but holds no record"
        for payload in records:
            if payload.get("schema_version") != SHARED_RECORD_SCHEMA_VERSION:
                missing.append(relative)
                break

    assert not missing, (
        "These shared records do not declare the current schema generation, so "
        f"a build too old to read them cannot tell: {sorted(set(missing))}"
    )


def test_the_boundary_stops_a_record_from_a_newer_build(workspace: Path) -> None:
    """The generation is only worth stamping if the boundary acts on it."""
    _write_one_of_every_shared_record(workspace)

    checked = 0
    for relative, path in _shared_files(workspace).items():
        if path.suffix not in RECORD_SUFFIXES:
            continue
        ahead = _with_generation(path, SHARED_RECORD_SCHEMA_VERSION + 1)
        with pytest.raises(SharedSchemaAheadError):
            validate_shared_file(relative, ahead)
        checked += 1
    assert checked


def test_no_record_carries_its_own_copy_of_the_generation() -> None:
    """One literal, so no kind can be raised on its own.

    A second literal is not a duplicate that reads badly, it is a fault: the
    boundary refuses any version above the one constant, on the sending side
    too, so raising one kind alone makes the device that wrote the record
    reject its own file and stop its own queue.
    """
    device_local = {
        # ``local/secrets.json`` records what generation of each secret this
        # machine holds. It is never shared, so it is not this generation.
        "guildbotics/utils/secret_store.py",
    }
    offenders: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT.parent).as_posix()
        if relative in device_local:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                names_it = (
                    isinstance(key, ast.Constant) and key.value == "schema_version"
                )
                literal = isinstance(value, ast.Constant) and isinstance(
                    value.value, int
                )
                if names_it and literal:
                    offenders.append(f"{relative}:{value.lineno}")

    assert not offenders, (
        "These stamp a schema generation of their own instead of "
        f"SHARED_RECORD_SCHEMA_VERSION: {offenders}"
    )


def _records_in(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if path.suffix == ".json":
        payload = json.loads(text)
    elif path.suffix in {".yml", ".yaml"}:
        payload = yaml.safe_load(text)
    else:
        return []
    return [payload] if isinstance(payload, dict) else []


def _with_generation(path: Path, version: int) -> bytes:
    """Return the file's bytes with every record's generation raised."""
    if path.suffix == ".jsonl":
        lines = [
            json.dumps({**json.loads(line), "schema_version": version})
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return ("\n".join(lines) + "\n").encode("utf-8")
    payload = _records_in(path)[0]
    payload["schema_version"] = version
    if path.suffix == ".json":
        return json.dumps(payload).encode("utf-8")
    return yaml.safe_dump(payload).encode("utf-8")
