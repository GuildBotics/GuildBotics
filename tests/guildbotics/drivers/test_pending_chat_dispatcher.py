from __future__ import annotations

import types

import pytest

from guildbotics.drivers.pending_chat_dispatcher import PendingChatDispatcher
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.runtime.event_listener import (
    INCOMING_CHAT_EVENT_KEY,
    IncomingChatEvent,
)


class _FakeContext:
    def __init__(self) -> None:
        self.logger = types.SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            debug=lambda *a, **k: None,
        )
        self.clones: list = []

    def clone_for(self, person):
        clone = types.SimpleNamespace(person=person, shared_state={})

        async def _aclose():
            clone.closed = True

        clone.aclose = _aclose
        self.clones.append(clone)
        return clone


def _event(event_id="E1", ts="100.1"):
    return ChatEvent(
        event_id=event_id,
        channel_id="C1",
        message_ts=ts,
        thread_ts="100.1",
        author_id="U1",
        text="hi",
    )


@pytest.mark.asyncio
async def test_dispatcher_runs_workflow_and_clears_pending(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(), "social")

    ran = []

    class _FakeRunner:
        def __init__(self, context, command, args):
            ran.append((context, command, args))

        async def run(self):
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.pending_chat_dispatcher.CommandRunner", _FakeRunner
    )

    context = _FakeContext()
    person = Person(person_id="alice", name="A", is_active=True)
    dispatcher = PendingChatDispatcher(context, state_store=store)  # type: ignore[arg-type]

    processed = await dispatcher.process_person(person)

    assert processed == 1
    # The event is marked processed and removed from the queue.
    assert store.is_processed_event("slack", "alice", "C1", "E1")
    assert store.load_pending_events("slack", "alice", "C1") == []
    # The workflow ran with the incoming event + participation in shared_state.
    ctx_used, command, _args = ran[0]
    assert command == "workflows/chat_conversation_workflow"
    incoming = IncomingChatEvent.from_shared_state(
        ctx_used.shared_state[INCOMING_CHAT_EVENT_KEY]
    )
    assert incoming is not None
    assert incoming.event.event_id == "E1"
    assert incoming.chat_participation == "social"


@pytest.mark.asyncio
async def test_dispatcher_skips_already_processed(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event())
    store.mark_processed_event("slack", "alice", "C1", "E1")

    class _FakeRunner:
        def __init__(self, *a):
            raise AssertionError("should not run an already-processed event")

        async def run(self):
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.pending_chat_dispatcher.CommandRunner", _FakeRunner
    )

    context = _FakeContext()
    person = Person(person_id="alice", name="A", is_active=True)
    dispatcher = PendingChatDispatcher(context, state_store=store)  # type: ignore[arg-type]

    processed = await dispatcher.process_person(person)

    assert processed == 0
    # The stale queued copy is cleaned up.
    assert store.load_pending_events("slack", "alice", "C1") == []


@pytest.mark.asyncio
async def test_dispatcher_leaves_event_queued_on_error(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event())

    class _FailingRunner:
        def __init__(self, *a):
            pass

        async def run(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "guildbotics.drivers.pending_chat_dispatcher.CommandRunner", _FailingRunner
    )

    context = _FakeContext()
    person = Person(person_id="alice", name="A", is_active=True)
    dispatcher = PendingChatDispatcher(context, state_store=store)  # type: ignore[arg-type]

    processed = await dispatcher.process_person(person)

    assert processed == 0
    # Failed event stays queued (and unprocessed) for a later retry pass.
    assert not store.is_processed_event("slack", "alice", "C1", "E1")
    assert [
        pe.event.event_id for pe in store.load_pending_events("slack", "alice", "C1")
    ] == ["E1"]
