"""What memory has to get right when another writer is working at the same time.

Two things that are not about the write itself. One is a decision taken from
what is on disk -- "is there already a team policy?" -- which is part of the
write whether or not it looks like one. The other is the audit journal, which
records what already happened and so must never be able to undo it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import guildbotics.utils.shared_write_lock as shared_write_lock_module
from guildbotics.capabilities import member_memory_audit
from guildbotics.capabilities.member_memory import MemberMemoryService
from guildbotics.entities.team import Person
from guildbotics.utils.shared_write_lock import (
    SharedWriteBusyError,
    shared_write_lock,
)


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    monkeypatch.setattr(shared_write_lock_module, "LOCK_TIMEOUT_SECONDS", 0.05)
    return tmp_path


@pytest.fixture
def service() -> MemberMemoryService:
    return MemberMemoryService(Person(person_id="p1", name="P"))


@contextmanager
def held_elsewhere(workspace: Path) -> Iterator[None]:
    """Hold the workspace's lock on another thread for the duration."""
    taken = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with shared_write_lock(workspace):
            taken.set()
            release.wait(30)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert taken.wait(30)
        yield
    finally:
        release.set()
        holder.join(30)


def test_the_team_policy_lookup_happens_inside_the_span(
    workspace: Path, service: MemberMemoryService
) -> None:
    """ "Is there one already?" decides create-or-replace, so it is the write.

    Asked outside the span it can be answered "no" a moment before the queue
    adopts another device's policy, and a second policy is then created beside
    it. Checked by refusal: with the lock held elsewhere, the call must not get
    as far as looking.
    """
    with held_elsewhere(workspace):
        with pytest.raises(SharedWriteBusyError):
            service.record(
                scope="team",
                title="Policy",
                body="rules",
                kind="policy",
                policy_approved=True,
            )

    assert not list((workspace / ".guildbotics/state/documents").rglob("meta.yml"))


def test_replacing_the_team_policy_does_not_deadlock_against_itself(
    service: MemberMemoryService,
) -> None:
    """Recording a second policy delegates to update, inside the same span.

    The delegation re-enters the lock rather than waiting for it. Before the
    lock re-entered, holding it across the lookup would have meant waiting out
    the full timeout and then reporting a workspace busy with itself.
    """
    first = service.record(
        scope="team", title="Policy", body="one", kind="policy", policy_approved=True
    )

    second = service.record(
        scope="team", title="Policy", body="two", kind="policy", policy_approved=True
    )

    assert second["doc_id"] == first["doc_id"]
    assert service.get(doc_id=first["doc_id"])["body"] == "two"


def test_a_busy_audit_journal_does_not_undo_the_document(
    monkeypatch: pytest.MonkeyPatch, service: MemberMemoryService, workspace: Path
) -> None:
    """The document is already written when the journal entry is made.

    Reporting the operation as failed would be untrue, and an agent that
    retries a `record` on that report creates a second document. The busy
    error is deliberately outside the ``OSError`` family, which is exactly why
    the handler that used to be enough no longer was.
    """
    calls: list[str] = []

    def busy(_self: object, _item: dict[str, object]) -> None:
        calls.append("record")
        raise SharedWriteBusyError("another writer holds the lock")

    monkeypatch.setattr(member_memory_audit.MemoryAuditStore, "record", busy)

    recorded = service.record(scope="personal", title="t", body="b")

    assert calls == ["record"]
    assert service.get(doc_id=recorded["doc_id"])["body"] == "b"


def test_a_busy_audit_journal_does_not_undo_an_update(
    monkeypatch: pytest.MonkeyPatch, service: MemberMemoryService
) -> None:
    """Same for the operation whose loss unit is an edit rather than a document."""
    recorded = service.record(scope="personal", title="t", body="b")

    def busy(_self: object, _item: dict[str, object]) -> None:
        raise SharedWriteBusyError("another writer holds the lock")

    monkeypatch.setattr(member_memory_audit.MemoryAuditStore, "record", busy)

    service.update(doc_id=recorded["doc_id"], body="edited")

    assert service.get(doc_id=recorded["doc_id"])["body"] == "edited"
