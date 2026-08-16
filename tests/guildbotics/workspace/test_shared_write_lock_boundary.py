"""A shared file cannot be changed without declaring the span it changes in.

This used to be a list. Every function that wrote a shared file was classified
here as taking the lock or not needing it, and the classification was the thing
that kept being wrong -- once because "does it read before writing?" is not the
question. The question is whether the change can land inside a window the
synchronization queue is working in, and the queue's windows cover the whole
shared tree, so for a writer of a shared path the answer is always yes.

So there is no list any more. Every write to a shared path goes through the
workspace sync port, and the port refuses one made outside the lock. What is
checked here is that refusal, and the two things a writer still has to get
right on its own: that its span covers the read its write is derived from, and
that a wider span from a caller subsumes a narrower one rather than deadlocking
against it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from guildbotics.capabilities.member_memory import MemberMemoryService
from guildbotics.capabilities.member_memory_audit import MemoryAuditStore
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.chat_state_store import (
    ChannelCursorState,
    PendingChatEvent,
    ScheduledPostState,
    ThreadConversationState,
    ThreadMessageState,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.observability.activity_event_store import ActivityEventStore
import guildbotics.utils.shared_write_lock as shared_write_lock_module
from guildbotics.utils.shared_write_lock import (
    SharedWriteBusyError,
    SharedWriteLockRequiredError,
    shared_write_lock,
)
from guildbotics.utils.workspace_sync_port import (
    append_shared_text,
    delete_shared_path,
    notify_shared_state_changed,
    write_shared_bytes,
    write_shared_json,
    write_shared_text,
)
from guildbotics.workspace.identity import publish_device_record


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def brief_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the wait so a refusal is immediate rather than half a minute.

    The production wait is generous because a first copy from a hub restores
    thousands of files inside the lock. Nothing here is testing how long a
    writer waits, only that it does.
    """
    monkeypatch.setattr(shared_write_lock_module, "LOCK_TIMEOUT_SECONDS", 0.05)


@contextmanager
def held_elsewhere(workspace: Path) -> Iterator[None]:
    """Hold the workspace's lock on another thread for the duration.

    Another thread rather than this one, because the lock re-enters within a
    thread: a writer called from here would join the test's own span and prove
    nothing. Another thread is also what the real other writer is -- a
    synchronization worker, or a second process.
    """
    taken = threading.Event()
    release = threading.Event()
    failure: list[BaseException] = []

    def hold() -> None:
        try:
            with shared_write_lock(workspace):
                taken.set()
                release.wait(30)
        except BaseException as exc:  # pragma: no cover - surfaced below
            failure.append(exc)
            taken.set()

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert taken.wait(30), "the holder thread never took the lock"
        assert not failure, failure
        yield
    finally:
        release.set()
        holder.join(30)
        assert not holder.is_alive()


#: Every way the port lets a caller change a shared path. Each one refuses a
#: change made without a span; that refusal is what replaced the list of
#: writers this file used to keep.
PORT_WRITES: dict[str, Callable[[Path], object]] = {
    "append_shared_text": lambda shared: append_shared_text(
        shared / "state/journal.jsonl", "{}\n"
    ),
    "delete_shared_path": lambda shared: delete_shared_path(shared / "state/gone.json"),
    "notify_shared_state_changed": lambda shared: notify_shared_state_changed(
        "update", [shared / "state/other.json"]
    ),
    "write_shared_bytes": lambda shared: write_shared_bytes(
        shared / "state/bytes.json", b"{}\n"
    ),
    "write_shared_json": lambda shared: write_shared_json(
        shared / "state/payload.json", {}
    ),
    "write_shared_text": lambda shared: write_shared_text(
        shared / "state/text.json", "{}\n"
    ),
}


def _existing_shared_files(workspace: Path) -> Path:
    shared = workspace / ".guildbotics"
    (shared / "state").mkdir(parents=True, exist_ok=True)
    (shared / "state/gone.json").write_text("{}\n", encoding="utf-8")
    (shared / "state/other.json").write_text("{}\n", encoding="utf-8")
    return shared


@pytest.mark.parametrize("helper", sorted(PORT_WRITES))
def test_the_port_refuses_a_shared_change_made_without_a_span(
    workspace: Path, helper: str
) -> None:
    """Checked on every entry, because one unguarded entry is the whole hole."""
    shared = _existing_shared_files(workspace)

    with pytest.raises(SharedWriteLockRequiredError):
        PORT_WRITES[helper](shared)


@pytest.mark.parametrize("helper", sorted(PORT_WRITES))
def test_the_same_change_inside_a_span_is_allowed(workspace: Path, helper: str) -> None:
    """The refusal is about the span, not about the path or the payload."""
    shared = _existing_shared_files(workspace)

    with shared_write_lock(workspace):
        PORT_WRITES[helper](shared)


def test_a_device_local_change_needs_no_span(workspace: Path) -> None:
    """``local/`` is this machine's, so no queue ever touches it.

    The port drops those paths rather than announcing them, and the lock has to
    agree: requiring a span for them would make every device-local writer take
    a lock that orders it against nothing.
    """
    local = workspace / ".guildbotics/local/hotkeys.yml"
    local.parent.mkdir(parents=True)

    assert write_shared_text(local, "a: b\n") is None
    assert local.read_text(encoding="utf-8") == "a: b\n"


def test_a_change_with_no_workspace_selected_needs_no_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is shared until a workspace is, so nothing is refused."""
    monkeypatch.delenv("GUILDBOTICS_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    path = tmp_path / ".guildbotics/state/thing.json"
    path.parent.mkdir(parents=True)

    assert write_shared_text(path, "{}\n") is None
    with shared_write_lock() as handle:
        assert handle is None


def test_a_wider_span_subsumes_a_narrower_one(workspace: Path) -> None:
    """A writer declares its own span without asking who called it.

    Answering "does my caller already hold it?" per writer is what produced a
    writer left outside the lock and, on the other side, writers that could
    only ever be called from one place. Re-entering removes the question.
    """
    service = MemberMemoryService(Person(person_id="p1", name="P"))

    with shared_write_lock(workspace):
        recorded = service.record(scope="personal", title="t", body="b")

    assert service.get(doc_id=recorded["doc_id"])["body"] == "b"


def test_a_span_still_excludes_another_thread(workspace: Path) -> None:
    """Re-entering is for this thread only; everyone else still waits."""
    with held_elsewhere(workspace):
        with pytest.raises(SharedWriteBusyError):
            with shared_write_lock(workspace):
                pytest.fail("the lock was taken while another thread held it")


def _chat_state() -> FileConversationStateStore:
    return FileConversationStateStore()


def _event() -> ChatEvent:
    return ChatEvent(
        event_id="e1",
        channel_id="C1",
        message_ts="1.0",
        thread_ts="1.0",
        author_id="U1",
        text="hi",
    )


#: Operations whose span has to cover a read as well as a write. A span that
#: covers only the write leaves the read outside, which looks protected and is
#: not: the value written was decided from content the queue may have replaced
#: in between.
READ_MODIFY_WRITE: dict[str, Callable[[Path], object]] = {
    "chat_state.append_thread_message": lambda _: _chat_state().append_thread_message(
        "slack",
        "p1",
        "C1",
        "1.0",
        ThreadMessageState(
            channel_id="C1", thread_ts="1.0", message_ts="1.0", author_id="U1", text="x"
        ),
    ),
    "chat_state.mark_processed_event": lambda _: _chat_state().mark_processed_event(
        "slack", "p1", "C1", "e1"
    ),
    "chat_state.remove_pending_event": lambda _: _chat_state().remove_pending_event(
        "slack", "p1", "C1", "e1"
    ),
    "chat_state.upsert_pending_event": lambda _: _chat_state().upsert_pending_event(
        "slack", "p1", "C1", _event()
    ),
    "identity.publish_device_record": publish_device_record,
    "memory_audit.record": lambda _: MemoryAuditStore().record(
        {"kind": "memory", "type": "memory.get", "message": "m"}
    ),
}


@pytest.mark.parametrize("operation", sorted(READ_MODIFY_WRITE))
def test_an_operation_that_reads_first_holds_the_lock_for_the_read(
    workspace: Path, operation: str
) -> None:
    """With the lock held elsewhere, the operation must not reach its read.

    Checked by refusal rather than by inspection: an operation that waits for
    the lock before reading cannot have derived its write from content that
    changed underneath it.
    """
    with held_elsewhere(workspace):
        with pytest.raises(SharedWriteBusyError):
            READ_MODIFY_WRITE[operation](workspace)


#: Operations that read nothing and still hold the lock, because what makes the
#: lock necessary is changing a path the queue is working in -- not reading one.
BLIND_WRITES: dict[str, Callable[[Path], object]] = {
    "activity.record": lambda _: ActivityEventStore().record(
        {"type": "github.pull_request.opened", "person_id": "p1"}
    ),
    "chat_state.save_channel_cursor": lambda _: _chat_state().save_channel_cursor(
        "slack", "p1", "C1", ChannelCursorState(cursor="c")
    ),
    "chat_state.save_pending_event": lambda _: _chat_state().save_pending_event(
        "slack",
        "p1",
        "C1",
        PendingChatEvent(event=_event(), chat_participation="strict"),
    ),
    "chat_state.save_receive_cutoff": lambda _: _chat_state().save_receive_cutoff(
        "slack", "p1", "1.0"
    ),
    "chat_state.save_scheduled_post_state": lambda _: (
        _chat_state().save_scheduled_post_state(
            "slack", "p1", "daily", ScheduledPostState(last_run_slot="s")
        )
    ),
    "chat_state.save_thread_state": lambda _: _chat_state().save_thread_state(
        "slack",
        "p1",
        "C1",
        "1.0",
        ThreadConversationState(channel_id="C1", thread_ts="1.0"),
    ),
    "task_runs.append": lambda _: RunStore().append("run-1", {"kind": "evidence"}),
}


@pytest.mark.parametrize("operation", sorted(BLIND_WRITES))
def test_a_writer_that_reads_nothing_still_holds_the_lock(
    workspace: Path, operation: str
) -> None:
    """The criterion is what the queue is doing, not what the writer read.

    A journal append reads nothing and is still committed by whatever the queue
    stages a moment later. Landing between the validation and the staging makes
    unchecked content into shared history, and the devices that receive it stop
    their queues on it. "It only appends" was the reasoning that left one of
    these outside the lock.
    """
    with held_elsewhere(workspace):
        with pytest.raises(SharedWriteBusyError):
            BLIND_WRITES[operation](workspace)
