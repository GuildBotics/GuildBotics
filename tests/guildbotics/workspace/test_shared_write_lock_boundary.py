"""Everything that changes a shared file changes it under the shared lock.

The rule is not "these particular stores take the lock" but "no writer of a
shared file can be added without deciding whether it needs one". A writer that
reads a file and writes it back outside the lock looks like an ordinary save in
review; what it costs appears on another machine, as a change that vanished
without being recorded as a conflict. Chat state loses an answer to a message,
or answers it twice; memory loses a whole document.

So the population is taken from the code rather than from a list someone
remembered to keep: every function in the package that calls one of the
shared-write helpers, found by reading the source. Each one is classified
below, and a new writer fails this file until it is. Reaching the helpers is
what makes the enumeration reliable -- a writer that opens the file itself
still has to announce the change to the sync port, so it is caught too.

Being classified as "the lock is taken at X" is a claim about X, so the second
half of this file makes every X prove it: with the lock already held, the
operation must give up rather than write.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

import pytest

import guildbotics.capabilities.member_memory as member_memory_module
import guildbotics.capabilities.member_memory_audit as memory_audit_module
import guildbotics.integrations.file_chat_state_store as chat_state_module
from guildbotics.capabilities.member_memory import MemberMemoryService
from guildbotics.capabilities.member_memory_audit import MemoryAuditStore
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
from guildbotics.workspace.shared_write_lock import (
    SharedWriteBusyError,
    shared_write_lock,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "guildbotics"

#: Calling one of these means the caller is changing a shared file, or telling
#: the sync queue that it just did. ``save_yaml_file`` is included because it
#: is how config is written, and ``notify_shared_state_changed`` because it is
#: how a writer that opens the file itself still reaches the queue.
SHARED_WRITE_HELPERS = {
    "delete_shared_path",
    "notify_shared_state_changed",
    "save_yaml_file",
    "write_shared_bytes",
    "write_shared_json",
    "write_shared_text",
}

#: The helpers' own definitions and the one function that forwards to them.
#: Nothing here writes on its own behalf.
PLUMBING = {
    "guildbotics.utils.fileio:save_yaml_file",
    "guildbotics.utils.workspace_sync_port:delete_shared_path",
    "guildbotics.utils.workspace_sync_port:write_shared_bytes",
    "guildbotics.utils.workspace_sync_port:write_shared_json",
    "guildbotics.utils.workspace_sync_port:write_shared_text",
}

#: Writers that run inside a block holding the lock, and where that block is.
#: The operation named here is the one the second half of this file checks.
UNDER_THE_LOCK = {
    "guildbotics.app_api.intelligences:IntelligenceConfigService._reconcile_mapping_file": (
        "app_api.config_revisions.apply_config_write"
    ),
    "guildbotics.app_api.intelligences:IntelligenceConfigService._reconcile_member_defs": (
        "app_api.config_revisions.apply_config_write"
    ),
    "guildbotics.app_api.intelligences:IntelligenceConfigService._write_cli_agent_def": (
        "app_api.config_revisions.apply_config_write"
    ),
    "guildbotics.app_api.intelligences:IntelligenceConfigService._write_model_def": (
        "app_api.config_revisions.apply_config_write"
    ),
    "guildbotics.app_api.intelligences:IntelligenceConfigService._write_native_agent_policy": (
        "app_api.config_revisions.apply_config_write"
    ),
    "guildbotics.app_api.intelligences:IntelligenceConfigService.update_config": (
        "app_api.config_revisions.apply_config_write"
    ),
    "guildbotics.capabilities.member_memory:MemberMemoryService._write_doc": (
        "MemberMemoryService.record / update"
    ),
    "guildbotics.capabilities.member_memory:MemberMemoryService._write_recent": (
        "MemberMemoryService.record / update / touch / archive / promote"
    ),
    "guildbotics.capabilities.member_memory:_notify_document_moved": (
        "MemberMemoryService.archive / promote"
    ),
    "guildbotics.capabilities.member_memory_audit:MemoryAuditStore.record": (
        "MemoryAuditStore.record itself"
    ),
    "guildbotics.editions.simple.setup_service:SimplePersonSetupService.update_person": (
        "app_api.config_revisions.apply_config_write"
    ),
    "guildbotics.editions.simple.setup_service:SimplePersonSetupService.write_person": (
        "app_api.config_revisions.apply_config_write"
    ),
    "guildbotics.editions.simple.setup_service:SimpleProjectSetupService.update_project": (
        "app_api.config_revisions.apply_config_write"
    ),
    "guildbotics.editions.simple.setup_service:SimpleProjectSetupService.write_project": (
        "app_api.config_revisions.apply_config_write"
    ),
    "guildbotics.editions.simple.setup_service:_write_default_person_id": (
        "app_api.config_revisions.apply_config_write"
    ),
    "guildbotics.integrations.file_chat_state_store:FileConversationStateStore._remove": (
        "every mutating method of FileConversationStateStore"
    ),
    "guildbotics.integrations.file_chat_state_store:FileConversationStateStore._write_json": (
        "every mutating method of FileConversationStateStore"
    ),
    "guildbotics.observability.session_transcripts:write_transcript_settings": (
        "app_api.config_revisions.apply_config_write"
    ),
    "guildbotics.utils.secret_store:KeyringSecretStore._write_index": (
        "cli.secrets, the only caller that changes the index"
    ),
}

#: Writers that do not need the lock, and why. A reason is required because the
#: next person to add a writer has to answer the same question, and "it looked
#: fine" is not an answer anyone can check.
WITHOUT_THE_LOCK = {
    "guildbotics.app_api.hotkeys:save_hotkeys": (
        "local/hotkeys.yml is device-specific and never shared"
    ),
    "guildbotics.capabilities.task_runs:RunStore.append": (
        "appends a line without reading the journal back, so nothing it wrote "
        "is derived from content the queue could have replaced"
    ),
    "guildbotics.observability.activity_event_store:ActivityEventStore.record": (
        "writes one new file per event and reads none of them"
    ),
    "guildbotics.utils.workspace_migrate:_convert_secrets_index": (
        "builds a workspace that is not selected yet, so it has no queue"
    ),
    "guildbotics.workspace.identity:ensure_workspace_identity": (
        "numbers the workspace once, under its own identity lock"
    ),
    "guildbotics.workspace.identity:publish_device_record": (
        "only this device writes its own record, from its local identity"
    ),
}


def _shared_write_sites() -> dict[str, list[int]]:
    """Return every function in the package that reaches a shared-write helper."""
    found: dict[str, list[int]] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        module = ".".join(path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts)
        enclosing: list[str] = []

        def visit(node: ast.AST) -> None:
            named = isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            )
            if named:
                enclosing.append(node.name)  # type: ignore[attr-defined]
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name in SHARED_WRITE_HELPERS and enclosing:
                    key = f"{module}:{'.'.join(enclosing)}"
                    found.setdefault(key, []).append(node.lineno)
            for child in ast.iter_child_nodes(node):
                visit(child)
            if named:
                enclosing.pop()

        visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


def test_every_writer_of_a_shared_file_is_classified() -> None:
    """A new shared writer fails here until someone decides about its lock."""
    writers = set(_shared_write_sites()) - PLUMBING
    classified = set(UNDER_THE_LOCK) | set(WITHOUT_THE_LOCK)

    unclassified = sorted(writers - classified)
    assert not unclassified, (
        "These write a shared file without saying whether they hold the "
        f"shared-write lock: {unclassified}"
    )

    stale = sorted(classified - writers)
    assert not stale, f"These are classified but no longer write anything: {stale}"


def test_no_writer_is_classified_both_ways() -> None:
    """The two tables answer the same question, so an entry belongs to one."""
    both = sorted(set(UNDER_THE_LOCK) & set(WITHOUT_THE_LOCK))
    assert not both, f"Classified as taking the lock and as not needing it: {both}"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    return tmp_path


def _impatient(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    """Make ``module``'s lock give up at once instead of after 30 seconds."""

    @contextmanager
    def brief(workspace_root: Path | None = None, **_: Any) -> Iterator[IO[str] | None]:
        with shared_write_lock(workspace_root, timeout=0.05) as handle:
            yield handle

    monkeypatch.setattr(module, "shared_write_lock", brief)


def _event(event_id: str = "e1") -> ChatEvent:
    return ChatEvent(
        event_id=event_id,
        channel_id="C1",
        message_ts="1.0",
        thread_ts="1.0",
        author_id="U1",
        text="hello",
    )


def _pending(event_id: str = "e1") -> PendingChatEvent:
    return PendingChatEvent(event=_event(event_id), chat_participation="strict")


#: Every method of the chat state store that changes a shared file. The store
#: reads the file it is about to write in most of them, so the lock has to span
#: both; here each one only has to prove it asks for the lock at all.
CHAT_STATE_MUTATIONS: dict[str, Callable[[FileConversationStateStore], None]] = {
    "save_channel_cursor": lambda store: store.save_channel_cursor(
        "slack", "p1", "C1", ChannelCursorState(cursor="c")
    ),
    "mark_processed_event": lambda store: store.mark_processed_event(
        "slack", "p1", "C1", "e1"
    ),
    "save_thread_state": lambda store: store.save_thread_state(
        "slack",
        "p1",
        "C1",
        "1.0",
        ThreadConversationState(channel_id="C1", thread_ts="1.0"),
    ),
    "append_thread_message": lambda store: store.append_thread_message(
        "slack",
        "p1",
        "C1",
        "1.0",
        ThreadMessageState(
            channel_id="C1", thread_ts="1.0", message_ts="1.0", author_id="U1", text="x"
        ),
    ),
    "save_scheduled_post_state": lambda store: store.save_scheduled_post_state(
        "slack", "p1", "daily", ScheduledPostState(last_run_slot="s")
    ),
    "upsert_pending_event": lambda store: store.upsert_pending_event(
        "slack", "p1", "C1", _event()
    ),
    "save_pending_event": lambda store: store.save_pending_event(
        "slack", "p1", "C1", _pending()
    ),
    "remove_pending_event": lambda store: store.remove_pending_event(
        "slack", "p1", "C1", "e1"
    ),
    "save_receive_cutoff": lambda store: store.save_receive_cutoff(
        "slack", "p1", "1.0"
    ),
    "clear_channel_receive_backlog": lambda store: store.clear_channel_receive_backlog(
        "slack", "p1", "C1"
    ),
}


@pytest.mark.parametrize("operation", sorted(CHAT_STATE_MUTATIONS))
def test_a_chat_state_change_waits_for_the_other_writer(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Checked on every mutating method, not one of them.

    The methods were written one at a time and only some of them read before
    writing, which is exactly how a writer comes to be left outside: whoever
    adds the next one copies the shape of a neighbour that happened not to
    need the lock.
    """
    store = FileConversationStateStore(
        base_dir=workspace / ".guildbotics/state/chat_state",
        cache_dir=workspace / ".guildbotics/local/chat-cache",
    )
    _impatient(monkeypatch, chat_state_module)

    with shared_write_lock(workspace):
        with pytest.raises(SharedWriteBusyError):
            CHAT_STATE_MUTATIONS[operation](store)


#: Every memory operation that changes a document. ``get`` and ``recall`` are
#: absent on purpose: they change only the audit journal, which takes the lock
#: on its own behalf below.
MEMORY_MUTATIONS: dict[str, Callable[[MemberMemoryService, str], None]] = {
    "record": lambda service, doc_id: service.record(
        scope="personal", title="t", body="b"
    ),
    "update": lambda service, doc_id: service.update(doc_id=doc_id, title="t2"),
    "touch": lambda service, doc_id: service.touch(doc_id=doc_id),
    "archive": lambda service, doc_id: service.archive(doc_id=doc_id),
    "promote": lambda service, doc_id: service.promote(doc_id=doc_id),
}


@pytest.mark.parametrize("operation", sorted(MEMORY_MUTATIONS))
def test_a_memory_change_waits_for_the_other_writer(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """What a memory change risks losing is a whole document, so all of them."""
    service = MemberMemoryService(Person(person_id="p1", name="P"))
    existing = service.record(scope="personal", title="seed", body="body")
    _impatient(monkeypatch, member_memory_module)

    with shared_write_lock(workspace):
        with pytest.raises(SharedWriteBusyError):
            MEMORY_MUTATIONS[operation](service, existing["doc_id"])


def test_an_audit_event_waits_for_the_other_writer(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journal is rewritten whole once it is full, and read to do it."""
    store = MemoryAuditStore()
    _impatient(monkeypatch, memory_audit_module)

    with shared_write_lock(workspace):
        with pytest.raises(SharedWriteBusyError):
            store.record({"kind": "memory", "type": "memory.get", "message": "m"})


def test_a_workspace_that_is_not_selected_has_nothing_to_serialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No workspace means no shared file and no queue, so the lock stands down.

    Writers here resolve the workspace the way the sync port does, and the port
    answers this state by dropping the change rather than raising. The lock has
    to answer it the same way, or every one of those writers grows its own copy
    of the exception.
    """
    monkeypatch.delenv("GUILDBOTICS_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)

    with shared_write_lock() as handle:
        assert handle is None
