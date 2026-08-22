"""A shared file is never written outside the workspace's shared-write lock.

This used to be a list of writers, each classified as taking the lock or not
needing it, and the classification was the thing that kept being wrong. Then it
was a refusal: the port raised at anyone who wrote without declaring a span,
and every writer of a shared path had to declare one -- which is the same list
again, spelled as a ``with`` in front of thirty functions and an entry here for
each.

Now the port takes the lock itself, so a writer that reads nothing has nothing
to declare and nothing to be listed as. What is left to check is what the port
cannot infer: that a value derived from a file's current contents was decided
inside the same span that wrote it. ``update_shared_text`` makes that structural
and is checked as such; the few operations whose span is wider than any single
write still say so by hand, and those are named below.

For those, waiting is not the property. The port takes the lock for the write
whichever way the caller declared its span, so an operation that read before
taking one reads happily and then raises at the write -- the same exception a
correct one would raise. The reads themselves are watched instead.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import guildbotics.utils.shared_write_lock as shared_write_lock_module
from guildbotics.capabilities.member_memory import MemberMemoryService
from guildbotics.capabilities.member_memory_audit import MemoryAuditStore
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.editions.simple.setup_service import SimpleProjectSetupService
from guildbotics.entities.team import Person
from guildbotics.utils.secret_store import KeyringSecretStore
from guildbotics.utils.shared_write_lock import (
    SharedWriteBusyError,
    shared_write_lock,
)
from guildbotics.utils.workspace_sync_port import (
    ChangeSet,
    append_shared_text,
    delete_shared_path,
    notify_shared_state_changed,
    set_workspace_sync_port,
    update_shared_json,
    update_shared_text,
    write_shared_bytes,
    write_shared_json,
    write_shared_text,
)
from tests.guildbotics.workspace.test_config_repository import shared_write_lock_is_held


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


#: Every way the port lets a caller change a shared path. Each one takes the
#: lock, which is what replaced the list of writers this file used to keep.
PORT_WRITES: dict[str, Callable[[Path], object]] = {
    "append_shared_text": lambda shared: append_shared_text(
        shared / "state/journal.jsonl", "{}\n"
    ),
    "delete_shared_path": lambda shared: delete_shared_path(shared / "state/gone.json"),
    "update_shared_json": lambda shared: update_shared_json(
        shared / "state/other.json", lambda current: current
    ),
    "update_shared_text": lambda shared: update_shared_text(
        shared / "state/other.json", lambda current: current
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


class _RecordingPort:
    """A sync port that keeps what it was told, so announcements can be counted."""

    def __init__(self, announced: list[ChangeSet]) -> None:
        self._announced = announced

    def shared_state_changed(self, change: ChangeSet) -> bool:
        self._announced.append(change)
        return True

    def await_pushed(self, change_id: str) -> bool:
        return False


def _existing_shared_files(workspace: Path) -> Path:
    shared = workspace / ".guildbotics"
    (shared / "state").mkdir(parents=True, exist_ok=True)
    (shared / "state/gone.json").write_text("{}\n", encoding="utf-8")
    (shared / "state/other.json").write_text("{}\n", encoding="utf-8")
    return shared


@pytest.mark.parametrize("helper", sorted(PORT_WRITES))
def test_every_port_write_waits_for_the_other_writer(
    workspace: Path, helper: str
) -> None:
    """Checked on every entry, because one unguarded entry is the whole hole."""
    shared = _existing_shared_files(workspace)

    with held_elsewhere(workspace):
        with pytest.raises(SharedWriteBusyError):
            PORT_WRITES[helper](shared)


@pytest.mark.parametrize("helper", sorted(PORT_WRITES))
def test_the_same_change_succeeds_when_nobody_else_is_writing(
    workspace: Path, helper: str
) -> None:
    """The wait is about the other writer, not about the path or the payload."""
    shared = _existing_shared_files(workspace)

    PORT_WRITES[helper](shared)


def test_announcing_a_change_already_made_takes_no_lock(workspace: Path) -> None:
    """Excluding the queue after the file changed protects nothing.

    A writer that renames or unlinks a shared file itself holds the lock across
    the change and this announcement together. Taking it here as well would
    only make an announcement look like the protection it is not, and would
    stall a writer already inside a wider span of its own.
    """
    shared = _existing_shared_files(workspace)

    with held_elsewhere(workspace):
        change = notify_shared_state_changed("update", [shared / "state/other.json"])

    assert change is not None and change.paths == ("state/other.json",)


def test_a_device_local_change_never_waits(workspace: Path) -> None:
    """``local/`` is this machine's, so no queue ever touches it.

    The port drops those paths rather than announcing them, and the lock has to
    agree: waiting on them would make every device-local writer queue behind a
    synchronization cycle it is not racing.
    """
    local = workspace / ".guildbotics/local/hotkeys.yml"
    local.parent.mkdir(parents=True)

    with held_elsewhere(workspace):
        assert write_shared_text(local, "a: b\n") is None

    assert local.read_text(encoding="utf-8") == "a: b\n"


def test_a_change_with_no_workspace_selected_never_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is shared until a workspace is, so there is nothing to serialize."""
    monkeypatch.delenv("GUILDBOTICS_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    path = tmp_path / ".guildbotics/state/thing.json"
    path.parent.mkdir(parents=True)

    assert write_shared_text(path, "{}\n") is None
    with shared_write_lock():
        pass


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


def test_a_rewrite_reads_the_file_inside_the_span(workspace: Path) -> None:
    """The transformation is not even reached while another writer holds it.

    This is the whole reason the closure is passed in rather than the finished
    text. Given the text, a caller has already read the file by the time it
    calls, and whether that read was covered is a matter of where the caller
    happened to put its ``with``. Given the transformation, the read is the
    helper's, and it happens after the wait.
    """
    shared = _existing_shared_files(workspace)
    reads: list[str | None] = []

    with held_elsewhere(workspace):
        with pytest.raises(SharedWriteBusyError):
            update_shared_text(
                shared / "state/other.json", lambda current: reads.append(current) or ""
            )

    assert not reads, "the file was read before the other writer was waited for"


def test_a_rewrite_sees_what_the_other_writer_left(workspace: Path) -> None:
    """Waiting is not enough on its own: the read has to come after it."""
    shared = _existing_shared_files(workspace)
    target = shared / "state/other.json"

    def hold_then_change() -> None:
        with shared_write_lock(workspace):
            started.set()
            proceed.wait(30)
            target.write_text(json.dumps({"n": 1}), encoding="utf-8")

    started = threading.Event()
    proceed = threading.Event()
    holder = threading.Thread(target=hold_then_change, daemon=True)
    holder.start()
    assert started.wait(30)
    proceed.set()
    holder.join(30)

    payload = update_shared_json(target, lambda current: {"n": current["n"] + 1})

    assert payload == {"n": 2}


def test_unchanged_content_is_not_rewritten(workspace: Path) -> None:
    """A file rewritten with what it already held is synchronization work with
    nothing behind it, so no change is announced for one."""
    shared = _existing_shared_files(workspace)
    target = shared / "state/other.json"
    announced: list[ChangeSet] = []
    set_workspace_sync_port(_RecordingPort(announced))
    try:
        assert update_shared_text(target, lambda current: current) == "{}\n"
        assert not announced

        update_shared_text(target, lambda _current: "{ }\n")
    finally:
        set_workspace_sync_port(None)

    assert [change.paths for change in announced] == [("state/other.json",)]


def test_a_rewrite_that_returns_nothing_removes_the_file(workspace: Path) -> None:
    """An emptied record is stored as no file, not as an empty one."""
    shared = _existing_shared_files(workspace)
    target = shared / "state/other.json"

    assert update_shared_text(target, lambda _current: None) is None
    assert not target.exists()


def _trim_the_audit_journal() -> None:
    """Fill the journal past its bound, so the entry after it rewrites the file.

    Below the bound an entry is appended without the file being read, and it is
    the rewrite that reads: the whole journal is loaded to decide what still
    fits. A small bound is what makes that the path under test.
    """
    store = MemoryAuditStore(max_file_bytes=400)
    for index in range(4):
        store.record({"kind": "memory", "type": "memory.get", "message": str(index)})


def _complete_a_run_with_evidence() -> object:
    """Complete a run that has what a completion requires.

    The evidence has to be there for the check to get past, because the check
    is the read this operation's span exists for.
    """
    store = RunStore()
    store.append("run-1", {"kind": "evidence", "evidence_type": "chat_reply"})
    return store.complete_run(
        "run-1",
        "done",
        "summary",
        subject_type="chat",
        subject_id="C1:1.0",
        person_id="p1",
    )


def _record_a_policy() -> object:
    """Record team policy, which is at most one document in the whole scope.

    Whether this creates or replaces comes from scanning the team directory,
    not from reading the file being written, so no write helper can put that
    read inside the span.
    """
    service = MemberMemoryService(Person(person_id="p1", name="P"))
    return service.record(
        scope="team", title="t", body="b", kind="policy", policy_approved=True
    )


def _set_the_default_member(root: Path) -> object:
    """Write the project's default member into the config it has to read first.

    Cleared rather than set, because the member being named is looked up in its
    own file and this is about the project config the value lands in.
    """
    config_dir = root / ".guildbotics/config"
    (config_dir / "team").mkdir(parents=True, exist_ok=True)
    project = config_dir / "team/project.yml"
    if not project.exists():
        project.write_text("language: en\n", encoding="utf-8")
    return SimpleProjectSetupService().set_default_person(
        config_dir=config_dir, person_id=""
    )


#: The operations whose span is wider than any one write, and which therefore
#: still declare it by hand. Each reads something the write helpers cannot see
#: it reading: a journal it may replace instead of appending to, another file
#: entirely, a directory.
HAND_DECLARED_SPANS: dict[str, Callable[[Path], object]] = {
    "memory.record_policy": lambda _: _record_a_policy(),
    "memory_audit.record": lambda _: _trim_the_audit_journal(),
    "secret_store.ensure_initialized": lambda root: KeyringSecretStore(
        root / ".guildbotics/config"
    ).ensure_initialized(),
    "setup_service.set_default_person": _set_the_default_member,
    "task_runs.complete_run": lambda _: _complete_a_run_with_evidence(),
}


@contextmanager
def recording_shared_reads(workspace: Path) -> Iterator[list[tuple[str, bool]]]:
    """Note every read of a shared file, and whether the lock was held for it.

    The port sees writes, so it takes the lock for one. It never sees a read --
    ``read_text`` on a memory document is an ordinary file read -- so nothing
    in the code can tell where a hand-declared span began. Watching the reads
    from the test is what makes "the span starts at the read" checkable at all.
    """
    seen: list[tuple[str, bool]] = []
    shared = workspace / ".guildbotics"
    originals = {
        name: getattr(Path, name) for name in ("read_text", "read_bytes", "open")
    }

    def note(path: Path) -> None:
        try:
            relative = path.resolve().relative_to(shared.resolve())
        except (OSError, ValueError):
            return
        if relative.parts and relative.parts[0] in {"config", "state"}:
            seen.append((relative.as_posix(), shared_write_lock_is_held(workspace)))

    def wrap(name: str) -> Callable[..., object]:
        original = originals[name]

        def wrapper(self: Path, *args: object, **kwargs: object) -> object:
            # ``open`` is how a journal is appended to as well as read, and a
            # write counted as a read would report the writer's own lock as
            # proof that its read was covered.
            mode = str(kwargs.get("mode", args[0] if args else "r"))
            if name != "open" or not set(mode) & set("wax+"):
                note(self)
            return original(self, *args, **kwargs)

        return wrapper

    patcher = pytest.MonkeyPatch()
    for name in originals:
        patcher.setattr(Path, name, wrap(name))
    try:
        yield seen
    finally:
        patcher.undo()


@pytest.mark.parametrize("operation", sorted(HAND_DECLARED_SPANS))
def test_a_hand_declared_span_starts_at_the_read(
    workspace: Path, operation: str
) -> None:
    """A read the write is derived from is inside the span, not in front of it.

    Asserting that the operation waits for another writer does not show this,
    and is the trap: the port takes the lock for the write whatever the caller
    did, so an operation whose read sits outside its span reads happily, then
    blocks at the write and raises the same exception as one that got it right.
    The two shapes are indistinguishable from the outside, which is why the
    reads are observed instead.

    It is the first read that is checked. An operation reads again afterwards
    to report what it did, and that read decided nothing.
    """
    # Run it once first: these operations read files they also create, and on
    # a workspace where it does not exist yet there is no read to observe.
    HAND_DECLARED_SPANS[operation](workspace)

    with recording_shared_reads(workspace) as reads:
        HAND_DECLARED_SPANS[operation](workspace)

    assert reads, f"{operation} read no shared file, so it does not belong here"
    path, held = reads[0]
    assert held, (
        f"{operation} read {path} before taking the lock, so what it wrote was "
        "decided from content the queue may have replaced in between"
    )
