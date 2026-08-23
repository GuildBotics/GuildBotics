"""Every shared-state write reaches the sync queue without the writer knowing.

Config, memory, conversation state, activity, and task runs must all announce
their writes through the same port, and none of them may take a revision
argument for it: the optimistic lock belongs to config alone.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from guildbotics.capabilities.member_memory import MemberMemoryService
from guildbotics.capabilities.member_memory_audit import MemoryAuditStore
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_state_store import (
    ChannelCursorState,
    ThreadMessageState,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.utils import workspace_sync_port
from guildbotics.utils.fileio import get_workspace_local_path, get_workspace_state_path
from tests.guildbotics.utils.test_workspace_sync_port import RecordingPort


@pytest.fixture
def person() -> Person:
    return Person(person_id="aiko", name="Aiko", person_type="agent")


def announced_paths(port: RecordingPort) -> list[str]:
    return [path for change in port.changes for path in change.paths]


def _moved(port: RecordingPort, doc_id: str) -> list[tuple[str, str]]:
    """Return what was announced for one document, in order."""
    return [
        (change.operation, path)
        for change in port.changes
        for path in change.paths
        if path.endswith(doc_id)
    ]


def test_activity_events_are_announced(port: RecordingPort) -> None:
    ActivityEventStore().record(
        {
            "event_id": "e1",
            "type": "github.push",
            "occurred_at": "2026-08-15T00:00:00Z",
        }
    )

    assert announced_paths(port) == ["state/events/2026/08/e1.json"]


def test_conversation_control_state_is_announced_but_the_cache_is_not(
    port: RecordingPort,
) -> None:
    store = FileConversationStateStore(
        base_dir=get_workspace_state_path("chat_state"),
        cache_dir=get_workspace_local_path("chat-cache"),
    )

    store.save_channel_cursor(
        "slack", "aiko", "C1", ChannelCursorState(cursor="cur-1", oldest_ts="1.0")
    )
    # This writes the device-local message cache and the shared thread state
    # that makes the thread discoverable after a handoff.
    store.append_thread_message("slack", "aiko", "C1", "T1", _thread_message())

    assert announced_paths(port) == [
        "state/chat_state/slack/aiko/channels/C1.json",
        "state/chat_state/slack/aiko/threads/C1/T1.json",
    ]
    assert get_workspace_local_path(
        "chat-cache", "slack", "aiko", "threads", "C1", "T1.json"
    ).is_file()


def test_removing_conversation_state_announces_a_delete(port: RecordingPort) -> None:
    store = FileConversationStateStore(base_dir=get_workspace_state_path("chat_state"))
    store.save_channel_cursor(
        "slack", "aiko", "C1", ChannelCursorState(cursor="cur-1", oldest_ts="1.0")
    )
    port.changes.clear()

    store.clear_channel_receive_backlog("slack", "aiko", "C1")

    assert all(change.operation == "delete" for change in port.changes)


def test_task_run_records_are_announced(port: RecordingPort) -> None:
    RunStore().append("run-1", {"kind": "evidence", "evidence_type": "pr_create"})

    assert announced_paths(port) == ["state/task-runs/run-1/result.json"]


def test_the_memory_audit_journal_is_announced(port: RecordingPort) -> None:
    MemoryAuditStore().record({"kind": "memory", "type": "memory.record"})

    assert announced_paths(port) == ["state/documents/memory_events.jsonl"]


def test_recording_memory_announces_the_document(
    person: Person, port: RecordingPort
) -> None:
    MemberMemoryService(person).record(scope="team", title="Note", body="body")

    paths = announced_paths(port)
    assert any(path.endswith("/meta.yml") for path in paths)
    assert any(path.endswith("/body.md") for path in paths)
    assert all(path.startswith("state/documents/") for path in paths)


def test_archiving_memory_announces_both_locations(
    person: Person, port: RecordingPort
) -> None:
    doc_id = MemberMemoryService(person).record(
        title="Note", body="body", scope="team"
    )["doc_id"]
    port.changes.clear()

    MemberMemoryService(person).archive(doc_id=doc_id, scope="team")

    assert _moved(port, doc_id) == [
        ("delete", f"state/documents/team/{doc_id}"),
        ("create", f"state/documents/team/archived/{doc_id}"),
    ]


def test_promoting_memory_announces_both_locations(
    person: Person, port: RecordingPort
) -> None:
    doc_id = MemberMemoryService(person).record(
        title="Note", body="body", scope="personal"
    )["doc_id"]
    port.changes.clear()

    MemberMemoryService(person).promote(doc_id=doc_id)

    assert _moved(port, doc_id) == [
        ("delete", f"state/documents/personal/{person.person_id}/{doc_id}"),
        ("create", f"state/documents/team/{doc_id}"),
    ]


@pytest.mark.parametrize(
    "method",
    [
        MemberMemoryService.record,
        MemberMemoryService.update,
        MemberMemoryService.touch,
        MemberMemoryService.archive,
        MemberMemoryService.promote,
        FileConversationStateStore.save_channel_cursor,
        FileConversationStateStore.save_thread_state,
        FileConversationStateStore.upsert_pending_event,
        RunStore.append,
        ActivityEventStore.record,
    ],
)
def test_saving_apis_did_not_gain_a_revision_argument(method: object) -> None:
    parameters = set(inspect.signature(method).parameters)  # type: ignore[arg-type]

    assert not parameters & {"blob_id", "expected_blob_id", "revision", "change_id"}


def test_a_workspace_local_write_never_reaches_the_queue(
    tmp_path: Path, port: RecordingPort
) -> None:
    workspace_sync_port.write_shared_text(
        get_workspace_local_path("run", "notes.txt"), "device only"
    )

    assert port.changes == []


def _thread_message() -> ThreadMessageState:
    return ThreadMessageState(
        channel_id="C1",
        thread_ts="T1",
        message_ts="1.0",
        author_id="U1",
        text="hello",
    )
