from __future__ import annotations

import types

import pytest

from guildbotics.drivers.execution import ExecutionCoordinator
from guildbotics.drivers.pending_chat_dispatcher import PendingChatDispatcher
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.intelligences.brains.cli_agent import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)
from guildbotics.observability import current_trace
from guildbotics.runtime.event_listener import (
    INCOMING_CHAT_EVENT_KEY,
    IncomingChatEvent,
)
from guildbotics.runtime.workflow_invocation import WORKFLOW_INVOCATION_KEY


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


def _event(event_id="E1", ts="100.1", thread_ts="100.1"):
    return ChatEvent(
        event_id=event_id,
        channel_id="C1",
        message_ts=ts,
        thread_ts=thread_ts,
        author_id="U1",
        text="hi",
    )


def _install_runner(monkeypatch, ran, *, fail_events=()):
    """CommandRunner stub recording dispatched event ids, failing selected ones."""

    class _Runner:
        def __init__(self, context, command, args):
            incoming = IncomingChatEvent.from_shared_state(
                context.shared_state[INCOMING_CHAT_EVENT_KEY]
            )
            assert incoming is not None
            self.event_id = incoming.event.event_id

        async def run(self):
            ran.append(self.event_id)
            if self.event_id in fail_events:
                raise RuntimeError("boom")
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _Runner
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
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _FakeRunner
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
    retry_context = ctx_used.shared_state[WORKFLOW_INVOCATION_KEY].payload[
        "retry_context"
    ]
    assert retry_context["attempt_count"] == 1
    assert retry_context["max_attempts"] == 5
    assert retry_context["is_final_attempt"] is False
    assert retry_context["run_id"]


@pytest.mark.asyncio
async def test_dispatcher_uses_env_for_initial_retry_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("GUILDBOTICS_CHAT_MAX_ATTEMPTS", "10")
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(), "social")
    ran = []

    class _FakeRunner:
        def __init__(self, context, command, args):
            ran.append(context)

        async def run(self):
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _FakeRunner
    )

    context = _FakeContext()
    person = Person(person_id="alice", name="A", is_active=True)
    dispatcher = PendingChatDispatcher(context, state_store=store)  # type: ignore[arg-type]

    await dispatcher.process_person(person)

    retry_context = (
        ran[0].shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"]
    )
    assert retry_context["max_attempts"] == 10


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
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _FakeRunner
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
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _FailingRunner
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
    pending = store.load_pending_events("slack", "alice", "C1")[0]
    assert pending.attempt_count == 1
    assert pending.next_attempt_at is not None
    first_run_id = pending.run_id

    processed = await dispatcher.process_person(person)

    assert processed == 0
    pending = store.load_pending_events("slack", "alice", "C1")[0]
    assert pending.attempt_count == 1
    assert pending.run_id == first_run_id


@pytest.mark.asyncio
async def test_dispatcher_failure_log_shares_workflow_trace(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event())
    workflow_traces = []

    class _FailingRunner:
        def __init__(self, *a):
            pass

        async def run(self):
            workflow_traces.append(current_trace())
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _FailingRunner
    )
    context = _FakeContext()
    logged = []
    context.logger.warning = lambda *a, **k: logged.append(current_trace())
    dispatcher = PendingChatDispatcher(context, state_store=store)  # type: ignore[arg-type]

    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    # The failure log is emitted inside the same trace the workflow ran under,
    # so diagnostics can correlate it instead of recording trace_id=null.
    assert len(logged) == 1 and logged[0] is not None
    assert workflow_traces[0] is not None
    assert logged[0].trace_id == workflow_traces[0].trace_id
    assert logged[0].person_id == "alice"
    assert logged[0].command == "workflows/chat_conversation_workflow"


@pytest.mark.asyncio
async def test_dispatcher_escalates_final_attempt_failure_to_error(
    monkeypatch, tmp_path
):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event())
    pending = store.load_pending_events("slack", "alice", "C1")[0]
    pending.attempt_count = 4
    pending.max_attempts = 5
    store.save_pending_event("slack", "alice", "C1", pending)

    class _FailingRunner:
        def __init__(self, *a):
            pass

        async def run(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _FailingRunner
    )
    context = _FakeContext()
    logged = []
    context.logger.warning = lambda *a, **k: logged.append(("warning",) + a)
    context.logger.error = lambda *a, **k: logged.append(("error",) + a)
    dispatcher = PendingChatDispatcher(context, state_store=store)  # type: ignore[arg-type]

    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    assert [entry[0] for entry in logged] == ["error"]
    assert logged[0][1].startswith("chat event abandoned after final attempt")
    # The abandoned event is terminalized so it can never block its thread.
    assert store.is_processed_event("slack", "alice", "C1", "E1")
    assert store.load_pending_events("slack", "alice", "C1") == []


@pytest.mark.asyncio
async def test_follower_in_same_thread_never_overtakes_backing_off_head(
    monkeypatch, tmp_path
):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event("EA", ts="100.2"))
    store.upsert_pending_event("slack", "alice", "C1", _event("EB", ts="100.3"))
    head = store.load_pending_events("slack", "alice", "C1")[0]
    head.attempt_count = 1
    head.next_attempt_at = "2999-01-01T00:00:00+00:00"
    head.last_error_category = "rate_limited"
    store.save_pending_event("slack", "alice", "C1", head)
    ran: list[str] = []
    _install_runner(monkeypatch, ran)
    dispatcher = PendingChatDispatcher(_FakeContext(), state_store=store)  # type: ignore[arg-type]

    processed = await dispatcher.process_person(
        Person(person_id="alice", name="A", is_active=True)
    )

    # The rate-limited head waits out its reset, and the follower must not run
    # ahead of it and advance the shared provider conversation.
    assert processed == 0
    assert ran == []
    assert [
        pe.event.event_id for pe in store.load_pending_events("slack", "alice", "C1")
    ] == ["EA", "EB"]


@pytest.mark.asyncio
async def test_follower_arrival_wakes_backing_off_head_once(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event("EA", ts="100.2"))
    store.upsert_pending_event("slack", "alice", "C1", _event("EB", ts="100.3"))
    head = store.load_pending_events("slack", "alice", "C1")[0]
    head.attempt_count = 1
    head.next_attempt_at = "2999-01-01T00:00:00+00:00"
    head.last_error_category = "failed"
    store.save_pending_event("slack", "alice", "C1", head)
    ran: list[str] = []
    _install_runner(monkeypatch, ran, fail_events={"EA"})
    dispatcher = PendingChatDispatcher(_FakeContext(), state_store=store)  # type: ignore[arg-type]
    person = Person(person_id="alice", name="A", is_active=True)

    await dispatcher.process_person(person)

    # The follower's arrival retried the head early (in FIFO order), and the
    # head's renewed failure still blocked the follower.
    assert ran == ["EA"]
    head = store.load_pending_events("slack", "alice", "C1")[0]
    assert head.event.event_id == "EA"
    assert head.wake_cursor == "100.3"
    assert head.next_attempt_at is not None

    # The same follower cannot wake the head again, even after a restart.
    restarted = PendingChatDispatcher(_FakeContext(), state_store=store)  # type: ignore[arg-type]
    await restarted.process_person(person)
    assert ran == ["EA"]


@pytest.mark.asyncio
async def test_follower_does_not_wake_head_with_unknown_error_category(
    monkeypatch, tmp_path
):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event("EA", ts="100.2"))
    store.upsert_pending_event("slack", "alice", "C1", _event("EB", ts="100.3"))
    head = store.load_pending_events("slack", "alice", "C1")[0]
    head.attempt_count = 1
    head.next_attempt_at = "2999-01-01T00:00:00+00:00"
    store.save_pending_event("slack", "alice", "C1", head)
    ran: list[str] = []
    _install_runner(monkeypatch, ran)
    dispatcher = PendingChatDispatcher(_FakeContext(), state_store=store)  # type: ignore[arg-type]

    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    # A backing-off head without a recorded error category (e.g. persisted
    # before the field existed) may be waiting out a provider rate limit, so
    # a follower arrival must not wake it early.
    assert ran == []
    assert store.load_pending_events("slack", "alice", "C1")[0].next_attempt_at


@pytest.mark.asyncio
async def test_head_failure_blocks_thread_but_not_other_threads(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event(
        "slack", "alice", "C1", _event("EA", ts="100.1", thread_ts="100.1")
    )
    store.upsert_pending_event(
        "slack", "alice", "C1", _event("EB", ts="100.2", thread_ts="100.1")
    )
    store.upsert_pending_event(
        "slack", "alice", "C1", _event("EC", ts="200.2", thread_ts="200.1")
    )
    ran: list[str] = []
    _install_runner(monkeypatch, ran, fail_events={"EA"})
    dispatcher = PendingChatDispatcher(_FakeContext(), state_store=store)  # type: ignore[arg-type]

    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    # EA fails: its follower EB stays queued, while the other thread's EC runs.
    assert ran == ["EA", "EC"]
    assert [
        pe.event.event_id for pe in store.load_pending_events("slack", "alice", "C1")
    ] == ["EA", "EB"]


@pytest.mark.asyncio
async def test_thread_follower_runs_after_head_terminalizes(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event("EA", ts="100.2"))
    store.upsert_pending_event("slack", "alice", "C1", _event("EB", ts="100.3"))
    head = store.load_pending_events("slack", "alice", "C1")[0]
    head.attempt_count = 5
    head.max_attempts = 5
    store.save_pending_event("slack", "alice", "C1", head)
    ran: list[str] = []
    _install_runner(monkeypatch, ran, fail_events={"EA"})
    context = _FakeContext()
    context.logger.error = lambda *a, **k: None
    dispatcher = PendingChatDispatcher(context, state_store=store)  # type: ignore[arg-type]
    person = Person(person_id="alice", name="A", is_active=True)

    await dispatcher.process_person(person)

    # The abandoned head is terminal and releases the thread; the follower is
    # still queued (never lost) and runs on the next pass.
    assert ran == ["EA"]
    assert store.is_processed_event("slack", "alice", "C1", "EA")
    assert [
        pe.event.event_id for pe in store.load_pending_events("slack", "alice", "C1")
    ] == ["EB"]

    await dispatcher.process_person(person)
    assert ran == ["EA", "EB"]
    assert store.load_pending_events("slack", "alice", "C1") == []


@pytest.mark.asyncio
async def test_thread_events_run_fifo_in_one_pass_when_head_succeeds(
    monkeypatch, tmp_path
):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event("EB", ts="100.3"))
    store.upsert_pending_event("slack", "alice", "C1", _event("EA", ts="100.2"))
    ran: list[str] = []
    _install_runner(monkeypatch, ran)
    dispatcher = PendingChatDispatcher(_FakeContext(), state_store=store)  # type: ignore[arg-type]

    processed = await dispatcher.process_person(
        Person(person_id="alice", name="A", is_active=True)
    )

    assert processed == 2
    assert ran == ["EA", "EB"]


@pytest.mark.asyncio
async def test_dispatcher_uses_provider_exact_rate_limit_reset(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event())
    retry_after_at = "2999-01-01T00:00:00+00:00"

    class _RateLimitedRunner:
        def __init__(self, *args):
            pass

        async def run(self):
            raise CliAgentExecutionError(
                cli_agent="codex",
                result=CliAgentExecutionResult(
                    stdout="",
                    stderr="rate limited",
                    returncode=1,
                    error_category="rate_limited",
                    error_details={"retry_after_at": retry_after_at},
                ),
            )

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner",
        _RateLimitedRunner,
    )
    dispatcher = PendingChatDispatcher(  # type: ignore[arg-type]
        _FakeContext(), state_store=store
    )

    assert (
        await dispatcher.process_person(
            Person(person_id="alice", name="A", is_active=True)
        )
        == 0
    )

    pending = store.load_pending_events("slack", "alice", "C1")[0]
    assert pending.attempt_count == 1
    assert pending.next_attempt_at == retry_after_at


@pytest.mark.asyncio
async def test_dispatcher_rejection_does_not_consume_retry_attempt(tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event())

    coordinator = ExecutionCoordinator()
    coordinator.begin_drain()
    context = _FakeContext()
    person = Person(person_id="alice", name="A", is_active=True)
    dispatcher = PendingChatDispatcher(
        context,  # type: ignore[arg-type]
        state_store=store,
        execution_coordinator=coordinator,
    )

    processed = await dispatcher.process_person(person)

    assert processed == 0
    # A dispatch rejected while the runtime drains never ran the workflow, so
    # it must not burn the event's retry budget.
    pending = store.load_pending_events("slack", "alice", "C1")[0]
    assert pending.attempt_count == 0


@pytest.mark.asyncio
async def test_dispatcher_skips_future_retry(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event())
    pending = store.load_pending_events("slack", "alice", "C1")[0]
    pending.next_attempt_at = "2999-01-01T00:00:00+00:00"
    store.save_pending_event("slack", "alice", "C1", pending)

    class _FakeRunner:
        def __init__(self, *a):
            raise AssertionError("future retry should not run")

        async def run(self):
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _FakeRunner
    )

    context = _FakeContext()
    person = Person(person_id="alice", name="A", is_active=True)
    dispatcher = PendingChatDispatcher(context, state_store=store)  # type: ignore[arg-type]

    processed = await dispatcher.process_person(person)

    assert processed == 0
    assert store.load_pending_events("slack", "alice", "C1")[0].next_attempt_at
