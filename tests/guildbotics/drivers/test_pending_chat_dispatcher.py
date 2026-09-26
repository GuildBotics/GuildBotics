from __future__ import annotations

import asyncio
import types

import pytest

from guildbotics.capabilities.chat_selection import ChatAttempt, ChatTurn
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.commands.metadata import CommandAccess
from guildbotics.drivers.execution import (
    ExecutionCoordinator,
    TaskRunCoordinator,
    WorkRejectedError,
)
from guildbotics.drivers.pending_chat_dispatcher import PendingChatDispatcher
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.intelligences.brains.cli_agent import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)
from guildbotics.observability import current_trace
from guildbotics.observability.trace_status import resolve_trace_status
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


@pytest.fixture(autouse=True)
def attempts(monkeypatch) -> list[ChatAttempt]:
    """Replace selection with one that sends every queued event to a turn."""
    seen: list[ChatAttempt] = []

    class _Selector:
        def __init__(self, context, *, command, state_store):
            pass

        async def prepare(self, *, service_name, channel_id, event, **_kwargs):
            return (service_name, channel_id, event)

        async def run(self, batch, attempt, run_turn):
            service_name, channel_id, event = batch
            seen.append(attempt)
            await run_turn(
                ChatTurn(
                    run_id=attempt.run_id,
                    attempt=attempt.attempt_count,
                    service_name=service_name,
                    channel_id=channel_id,
                    thread_ts=event.thread_ts,
                    event_id=event.event_id,
                    message_ts=event.message_ts,
                    work_identity=event.event_id,
                    context_cursor=event.message_ts,
                    prompt={},
                )
            )

    monkeypatch.setattr(
        "guildbotics.drivers.pending_chat_dispatcher.ChatSelector", _Selector
    )
    return seen


def _turn(context) -> ChatTurn:
    return ChatTurn.model_validate(
        context.shared_state[WORKFLOW_INVOCATION_KEY].payload
    )


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
        access = CommandAccess()

        def __init__(self, context, command, args):
            self.event_id = _turn(context).event_id

        async def run(self):
            ran.append(self.event_id)
            if self.event_id in fail_events:
                raise RuntimeError("boom")
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _Runner
    )


@pytest.mark.asyncio
async def test_dispatcher_runs_workflow_and_clears_pending(
    monkeypatch, tmp_path, attempts
):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(), "social")

    ran = []

    class _FakeRunner:
        access = CommandAccess()

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
    # The workflow ran the turn selection built for the event, on this attempt.
    ctx_used, command, _args = ran[0]
    assert command == "workflows/chat_conversation_workflow"
    turn = _turn(ctx_used)
    assert turn.event_id == "E1"
    assert turn.attempt == 1
    assert turn.run_id
    assert attempts == [
        ChatAttempt(run_id=turn.run_id, attempt_count=1, max_attempts=5)
    ]


@pytest.mark.asyncio
async def test_dispatcher_runs_same_chat_event_for_each_member(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path / "chat-state")
    for person_id in ("yuki", "aiko"):
        store.upsert_pending_event("slack", person_id, "C1", _event(), "strict")

    ran: list[str] = []

    class _FakeRunner:
        access = CommandAccess()

        def __init__(self, context, command, args):
            self.person_id = context.person.person_id

        async def run(self):
            ran.append(self.person_id)
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _FakeRunner
    )
    dispatcher = PendingChatDispatcher(
        _FakeContext(),  # type: ignore[arg-type]
        state_store=store,
        execution_coordinator=TaskRunCoordinator(),
    )

    for person_id in ("yuki", "aiko"):
        processed = await dispatcher.process_person(
            Person(person_id=person_id, name=person_id, is_active=True)
        )
        assert processed == 1

    assert ran == ["yuki", "aiko"]


@pytest.mark.asyncio
async def test_dispatcher_only_claims_the_event_the_workflow_records_the_run(
    monkeypatch, tmp_path
):
    """A batch the member does not act on leaves no run record.

    Whether an event holds work is decided by the workflow after it has read
    the thread, so the boundary claims the identity and the workflow records
    the start; a workflow that ran and declined leaves the history untouched.
    """
    store = FileConversationStateStore(base_dir=tmp_path / "chat-state")
    store.upsert_pending_event("slack", "alice", "C1", _event(), "strict")
    ran: list[str] = []
    _install_runner(monkeypatch, ran)
    dispatcher = PendingChatDispatcher(
        _FakeContext(),  # type: ignore[arg-type]
        state_store=store,
        execution_coordinator=TaskRunCoordinator(),
    )

    processed = await dispatcher.process_person(
        Person(person_id="alice", name="A", is_active=True)
    )

    assert processed == 1
    assert ran == ["E1"]
    assert list(RunStore().records()) == []


@pytest.mark.asyncio
async def test_dispatcher_finishes_the_run_the_workflow_started(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path / "chat-state")
    store.upsert_pending_event("slack", "alice", "C1", _event(), "strict")

    class _Runner:
        access = CommandAccess()

        def __init__(self, context, command, args):
            self.run_id = _turn(context).run_id

        async def run(self):
            RunStore().start_record(
                self.run_id,
                work_kind="workflows/chat_conversation_workflow",
                execution_mode="autonomous",
                member_id="alice",
            )
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _Runner
    )
    dispatcher = PendingChatDispatcher(
        _FakeContext(),  # type: ignore[arg-type]
        state_store=store,
        execution_coordinator=TaskRunCoordinator(),
    )

    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    records = list(RunStore().records())
    assert [record.status for record in records] == ["succeeded"]
    assert records[0].finished_at is not None


@pytest.mark.asyncio
async def test_dispatcher_tracks_work_under_its_trace_id(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(), "social")
    execution = ExecutionCoordinator()
    seen: list[tuple[str | None, list[str]]] = []

    class _Runner:
        access = CommandAccess()

        def __init__(self, context, command, args):
            pass

        async def run(self):
            trace = current_trace()
            works = execution.snapshot()
            seen.append((trace.trace_id if trace else None, [w.id for w in works]))
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _Runner
    )

    context = _FakeContext()
    person = Person(person_id="alice", name="A", is_active=True)
    dispatcher = PendingChatDispatcher(
        context,  # type: ignore[arg-type]
        state_store=store,
        execution_coordinator=execution,
    )

    await dispatcher.process_person(person)

    trace_id, work_ids = seen[0]
    assert trace_id is not None
    assert work_ids == [trace_id]


def _capture_boundary_events(monkeypatch) -> list[dict]:
    """Collect the trace boundary events ``command_boundary`` records."""
    recorded: list[dict] = []

    def _record(*, event_type, payload, **_kwargs):
        recorded.append({"kind": "event", "type": event_type, "payload": payload})

    monkeypatch.setattr("guildbotics.drivers.utils.record_correlated_event", _record)
    return recorded


@pytest.mark.asyncio
async def test_dispatch_records_the_trace_boundary_around_the_turn(
    monkeypatch, tmp_path
):
    # The dispatcher opens this trace, so it is the only layer that can say
    # the run started and ended. Without the boundary, the first
    # ``span.finished`` of the run -- an LLM decision made before the agent
    # turn even starts -- was read as the whole run succeeding.
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(), "social")
    recorded = _capture_boundary_events(monkeypatch)
    mid_turn: list[str] = []

    class _Runner:
        access = CommandAccess()

        def __init__(self, context, command, args):
            pass

        async def run(self):
            llm_decision = {"kind": "event", "type": "span.finished"}
            mid_turn.append(resolve_trace_status([*recorded, llm_decision]))
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _Runner
    )

    dispatcher = PendingChatDispatcher(_FakeContext(), state_store=store)  # type: ignore[arg-type]
    assert (
        await dispatcher.process_person(
            Person(person_id="alice", name="A", is_active=True)
        )
        == 1
    )

    assert [item["type"] for item in recorded] == [
        "command.started",
        "command.finished",
    ]
    assert mid_turn == ["running"]
    assert resolve_trace_status(recorded) == "success"


def _install_selection(monkeypatch, prepare) -> list:
    """Replace selection with ``prepare``; record the trace each step ran in."""
    traces: list = []

    class _Selector:
        def __init__(self, context, *, command, state_store):
            pass

        async def prepare(self, **kwargs):
            traces.append(("prepare", current_trace()))
            return prepare()

        async def run(self, batch, attempt, run_turn):
            traces.append(("run", current_trace()))

    monkeypatch.setattr(
        "guildbotics.drivers.pending_chat_dispatcher.ChatSelector", _Selector
    )
    return traces


@pytest.mark.asyncio
async def test_event_that_is_not_work_leaves_no_trace(monkeypatch, tmp_path):
    # An event selection declines before judgment (an edit, the member's own
    # message, one its participation excludes) used to leave a
    # ``command.started`` / ``command.finished`` trace with nothing in it,
    # once per member, that could not even say why nothing happened.
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(), "social")
    recorded = _capture_boundary_events(monkeypatch)
    traces = _install_selection(monkeypatch, lambda: None)

    dispatcher = PendingChatDispatcher(_FakeContext(), state_store=store)  # type: ignore[arg-type]
    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    assert traces == [("prepare", None)]
    assert recorded == []
    assert store.is_processed_event("slack", "alice", "C1", "E1")
    assert store.load_pending_events("slack", "alice", "C1") == []


@pytest.mark.asyncio
async def test_selection_failure_is_traced_and_retried(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(), "social")
    recorded = _capture_boundary_events(monkeypatch)

    def _fail():
        raise RuntimeError("invalid_auth")

    traces = _install_selection(monkeypatch, _fail)

    dispatcher = PendingChatDispatcher(_FakeContext(), state_store=store)  # type: ignore[arg-type]
    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    assert traces == [("prepare", None)]
    assert [item["type"] for item in recorded] == ["command.started", "command.failed"]
    [pending] = store.load_pending_events("slack", "alice", "C1")
    assert pending.attempt_count == 1
    assert pending.last_error_category == "failed"


@pytest.mark.asyncio
async def test_unavailable_chat_service_is_traced_and_retried(monkeypatch, tmp_path):
    # Building selection resolves the member's chat service, which fails when
    # its token is missing. That failure spends an attempt like any other, so
    # the event backs off and is eventually abandoned instead of failing on
    # every poll forever.
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(), "social")
    recorded = _capture_boundary_events(monkeypatch)

    class _Selector:
        def __init__(self, context, *, command, state_store):
            raise RuntimeError("SLACK_BOT_TOKEN is not set")

    monkeypatch.setattr(
        "guildbotics.drivers.pending_chat_dispatcher.ChatSelector", _Selector
    )

    dispatcher = PendingChatDispatcher(_FakeContext(), state_store=store)  # type: ignore[arg-type]
    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    assert [item["type"] for item in recorded] == ["command.started", "command.failed"]
    [pending] = store.load_pending_events("slack", "alice", "C1")
    assert pending.attempt_count == 1
    assert pending.next_attempt_at


@pytest.mark.asyncio
async def test_selection_failure_on_the_final_attempt_abandons_the_event(
    monkeypatch, tmp_path
):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(), "social")
    [pending] = store.load_pending_events("slack", "alice", "C1")
    pending.attempt_count = 4
    pending.max_attempts = 5
    store.save_pending_event("slack", "alice", "C1", pending)
    recorded = _capture_boundary_events(monkeypatch)
    abandoned: list[dict] = []
    monkeypatch.setattr(
        "guildbotics.drivers.pending_chat_dispatcher.record_chat_dispatch_abandoned",
        lambda **kwargs: abandoned.append(kwargs),
    )

    def _fail():
        raise RuntimeError("invalid_auth")

    _install_selection(monkeypatch, _fail)
    context = _FakeContext()
    context.logger.error = lambda *a, **k: None

    dispatcher = PendingChatDispatcher(context, state_store=store)  # type: ignore[arg-type]
    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    assert [item["type"] for item in recorded] == ["command.started", "command.failed"]
    assert [item["attempt_count"] for item in abandoned] == [5]
    assert store.is_processed_event("slack", "alice", "C1", "E1")
    assert store.load_pending_events("slack", "alice", "C1") == []


@pytest.mark.asyncio
async def test_selected_event_runs_under_a_trace_naming_its_thread(
    monkeypatch, tmp_path
):
    # Reaction-only and no-op judgments never reach the workflow, so the
    # trace itself names the thread the run is about.
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(ts="101.1"), "social")
    traces = _install_selection(monkeypatch, lambda: object())

    dispatcher = PendingChatDispatcher(
        _FakeContext(),  # type: ignore[arg-type]
        state_store=store,
        service_run_id="service-1",
    )
    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    step, trace = traces[-1]
    assert step == "run"
    assert trace.attributes == {
        "service_run_id": "service-1",
        "event.provider": "slack",
        "slack.channel": "C1",
        "slack.thread_ts": "100.1",
        "slack.ts": "101.1",
        "event_id": "E1",
    }
    assert store.is_processed_event("slack", "alice", "C1", "E1")


@pytest.mark.asyncio
async def test_dispatch_records_a_failed_boundary_and_still_retries(
    monkeypatch, tmp_path
):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(), "social")
    recorded = _capture_boundary_events(monkeypatch)
    _install_runner(monkeypatch, [], fail_events=("E1",))

    dispatcher = PendingChatDispatcher(_FakeContext(), state_store=store)  # type: ignore[arg-type]
    assert (
        await dispatcher.process_person(
            Person(person_id="alice", name="A", is_active=True)
        )
        == 0
    )

    assert [item["type"] for item in recorded] == ["command.started", "command.failed"]
    assert resolve_trace_status(recorded) == "failed"
    # The failure still reaches the dispatcher's own retry handling.
    assert store.load_pending_events("slack", "alice", "C1")[0].last_error_category == (
        "failed"
    )


@pytest.mark.asyncio
async def test_cancelled_dispatch_records_a_failed_boundary(monkeypatch, tmp_path):
    # Stopping the service cancels the cycle this dispatch runs in.
    # ``CancelledError`` is not an ``Exception``, so a boundary that only
    # caught ``Exception`` left ``command.started`` as the trace's last record
    # and the run read as still going after the service had stopped.
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(), "social")
    recorded = _capture_boundary_events(monkeypatch)

    class _Runner:
        access = CommandAccess()

        def __init__(self, context, command, args):
            pass

        async def run(self):
            raise asyncio.CancelledError

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _Runner
    )

    dispatcher = PendingChatDispatcher(_FakeContext(), state_store=store)  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.process_person(
            Person(person_id="alice", name="A", is_active=True)
        )

    assert [item["type"] for item in recorded] == ["command.started", "command.failed"]
    # Classified as a cancellation, so stopping the service raises no Desktop
    # execution alert for the work it drained.
    assert recorded[-1]["payload"]["code"] == "cancelled"
    assert resolve_trace_status(recorded) == "failed"
    # The event is still queued: cancellation is not a consumed attempt.
    assert store.load_pending_events("slack", "alice", "C1")


@pytest.mark.asyncio
async def test_rejected_dispatch_records_no_boundary(monkeypatch, tmp_path):
    # Work rejected before it starts never ran, so it must not leave a
    # half-open execution that reads as still running.
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(), "social")
    recorded = _capture_boundary_events(monkeypatch)

    class _RejectingCoordinator(ExecutionCoordinator):
        def track_work(self, **kwargs):
            raise WorkRejectedError("rejected", reason="duplicate")

    dispatcher = PendingChatDispatcher(
        _FakeContext(),  # type: ignore[arg-type]
        state_store=store,
        execution_coordinator=_RejectingCoordinator(),
    )
    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    assert recorded == []


@pytest.mark.asyncio
async def test_dispatcher_uses_env_for_initial_retry_budget(
    monkeypatch, tmp_path, attempts
):
    monkeypatch.setenv("GUILDBOTICS_CHAT_MAX_ATTEMPTS", "10")
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event(), "social")
    ran = []

    class _FakeRunner:
        access = CommandAccess()

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

    assert [attempt.max_attempts for attempt in attempts] == [10]


@pytest.mark.asyncio
async def test_dispatcher_skips_already_processed(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event())
    store.mark_processed_event("slack", "alice", "C1", "E1")

    class _FakeRunner:
        access = CommandAccess()

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
        access = CommandAccess()

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
        access = CommandAccess()

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
        access = CommandAccess()

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
async def test_dispatcher_reloads_thread_after_each_workflow_snapshot(
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

    assert processed == 1
    assert ran == ["EA"]
    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))
    assert ran == ["EA", "EB"]


@pytest.mark.asyncio
async def test_dispatcher_uses_provider_exact_rate_limit_reset(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event())
    retry_after_at = "2999-01-01T00:00:00+00:00"

    class _RateLimitedRunner:
        access = CommandAccess()

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
        access = CommandAccess()

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


@pytest.mark.asyncio
async def test_dispatch_failure_records_retry_scheduled_event(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event())
    ran: list[str] = []
    _install_runner(monkeypatch, ran, fail_events={"E1"})
    recorded: list[dict] = []
    monkeypatch.setattr(
        "guildbotics.drivers.pending_chat_dispatcher."
        "record_chat_dispatch_retry_scheduled",
        lambda **kwargs: recorded.append(kwargs),
    )
    dispatcher = PendingChatDispatcher(_FakeContext(), state_store=store)  # type: ignore[arg-type]

    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    assert len(recorded) == 1
    event = recorded[0]
    assert event["event_id"] == "E1"
    assert event["run_id"]
    assert event["attempt_count"] == 1
    assert event["next_attempt_at"]
    assert event["error_category"] == "failed"


@pytest.mark.asyncio
async def test_thread_context_unavailable_keeps_event_pending_forever(
    monkeypatch, tmp_path
):
    """A provider outage never consumes retry budget or abandons the event."""
    from guildbotics.integrations.chat_state_store import ThreadContextUnavailableError

    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event())
    pending = store.load_pending_events("slack", "alice", "C1")[0]
    pending.attempt_count = 5
    pending.max_attempts = 5
    store.save_pending_event("slack", "alice", "C1", pending)

    class _UnavailableRunner:
        access = CommandAccess()

        def __init__(self, *a):
            pass

        async def run(self):
            raise ThreadContextUnavailableError("provider down")

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _UnavailableRunner
    )
    abandoned: list[dict] = []
    monkeypatch.setattr(
        "guildbotics.drivers.pending_chat_dispatcher.record_chat_dispatch_abandoned",
        lambda **kwargs: abandoned.append(kwargs),
    )
    context = _FakeContext()
    context.logger.warning = lambda *a, **k: None
    dispatcher = PendingChatDispatcher(context, state_store=store)  # type: ignore[arg-type]

    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    assert abandoned == []
    assert not store.is_processed_event("slack", "alice", "C1", "E1")
    pending = store.load_pending_events("slack", "alice", "C1")[0]
    # The consumed attempt is handed back so the event can wait indefinitely.
    assert pending.attempt_count == 5
    assert pending.last_error_category == "provider_unavailable"
    assert pending.next_attempt_at is not None


@pytest.mark.asyncio
async def test_dispatcher_side_abandon_records_abandoned_event(monkeypatch, tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event("slack", "alice", "C1", _event())
    pending = store.load_pending_events("slack", "alice", "C1")[0]
    pending.attempt_count = 5
    pending.max_attempts = 5
    store.save_pending_event("slack", "alice", "C1", pending)
    ran: list[str] = []
    _install_runner(monkeypatch, ran, fail_events={"E1"})
    recorded: list[dict] = []
    monkeypatch.setattr(
        "guildbotics.drivers.pending_chat_dispatcher.record_chat_dispatch_abandoned",
        lambda **kwargs: recorded.append(kwargs),
    )
    context = _FakeContext()
    context.logger.error = lambda *a, **k: None
    dispatcher = PendingChatDispatcher(context, state_store=store)  # type: ignore[arg-type]

    await dispatcher.process_person(Person(person_id="alice", name="A", is_active=True))

    assert len(recorded) == 1
    assert recorded[0]["event_id"] == "E1"
    assert recorded[0]["attempt_count"] == 6
    assert recorded[0]["max_attempts"] == 5
    # record_chat_dispatch_abandoned itself caps the diagnostics payload's
    # attempt_count at max_attempts (this monkeypatch captures the dispatcher's
    # raw call args, which still legitimately go one over before capping).


@pytest.mark.asyncio
async def test_failed_event_runs_again_once_its_retry_time_arrives(
    monkeypatch, tmp_path
):
    """A failed attempt must really be retried, not swallowed as a duplicate.

    The first attempt records a terminal TaskRun under the event's stable work
    identity. The retry enters the same coordinator with the same identity, so
    it is the real ``TaskRunCoordinator`` -- not a stub -- that has to tell a
    failed attempt apart from an event another device already handled.
    """
    store = FileConversationStateStore(base_dir=tmp_path / "chat-state")
    store.upsert_pending_event("slack", "alice", "C1", _event())
    ran: list[int] = []

    class _FailsOnceRunner:
        access = CommandAccess()

        def __init__(self, *a):
            pass

        async def run(self):
            ran.append(len(ran) + 1)
            if len(ran) == 1:
                raise RuntimeError("boom")
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _FailsOnceRunner
    )
    person = Person(person_id="alice", name="A", is_active=True)
    dispatcher = PendingChatDispatcher(
        _FakeContext(),  # type: ignore[arg-type]
        state_store=store,
        execution_coordinator=TaskRunCoordinator(),
    )

    assert await dispatcher.process_person(person) == 0
    pending = store.load_pending_events("slack", "alice", "C1")[0]
    assert pending.attempt_count == 1
    # Reaching the scheduled retry time is the only thing the queue waits for.
    pending.next_attempt_at = "2000-01-01T00:00:00+00:00"
    store.save_pending_event("slack", "alice", "C1", pending)

    assert await dispatcher.process_person(person) == 1

    assert ran == [1, 2]
    assert store.is_processed_event("slack", "alice", "C1", "E1")
    assert store.load_pending_events("slack", "alice", "C1") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_ids", [[], ["E1", "E2"]])
@pytest.mark.parametrize(
    "updates_action",
    [
        None,
        "chat_reply",
        "chat_noop",
        "blocked",
        "issue_comment",
        "git_publish",
        "git_push",
        "issue_update",
    ],
)
async def test_event_completed_elsewhere_is_processed_without_running_again(
    monkeypatch, tmp_path, batch_ids, updates_action
):
    """An event whose run recorded a result stays a duplicate, as before."""
    store = FileConversationStateStore(base_dir=tmp_path / "chat-state")
    store.upsert_pending_event("slack", "alice", "C1", _event())
    if batch_ids:
        store.upsert_pending_event("slack", "alice", "C1", _event("E2", ts="101.1"))
    run_store = RunStore()
    run_store.start_record(
        "prior-run",
        work_kind="workflows/chat_conversation_workflow",
        execution_mode="autonomous",
        member_id="alice",
        work_identity={
            "kind": "chat-event",
            "event_id": "E1",
            "service": "slack",
            "channel_id": "C1",
        },
    )
    run_store.append_evidence("prior-run", "chat_reply", {"text": "answered"})
    if batch_ids:
        run_store.append_evidence("prior-run", "chat_batch", {"event_ids": batch_ids})
    if updates_action:
        store.upsert_pending_event("slack", "alice", "C1", _event("E3", ts="102.1"))
        run_store.append_evidence("prior-run", "chat_updates", {"event_ids": ["E3"]})
        if updates_action != "blocked":
            run_store.append_evidence("prior-run", updates_action, {"text": "decision"})
    run_store.complete_run(
        "prior-run",
        "blocked" if updates_action == "blocked" else "done",
        "answered",
        subject_type="chat",
        subject_id="slack:C1:100.1",
        person_id="alice",
    )

    class _NeverRuns:
        access = CommandAccess()

        def __init__(self, *a):
            raise AssertionError("a completed event must not run again")

        async def run(self):
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.workflow_dispatcher.CommandRunner", _NeverRuns
    )
    dispatcher = PendingChatDispatcher(
        _FakeContext(),  # type: ignore[arg-type]
        state_store=store,
        execution_coordinator=TaskRunCoordinator(),
    )

    processed = await dispatcher.process_person(
        Person(person_id="alice", name="A", is_active=True)
    )

    assert processed == 1
    assert store.is_processed_event("slack", "alice", "C1", "E1")
    assert [
        item.event.event_id
        for item in store.load_pending_events("slack", "alice", "C1")
    ] == (["E3"] if updates_action in {"chat_noop", "blocked"} else [])


@pytest.mark.asyncio
async def test_repeated_failures_use_the_whole_attempt_budget_then_abandon(
    monkeypatch, tmp_path
):
    """The budget is spent on real attempts, and only its end abandons the event."""
    monkeypatch.setenv("GUILDBOTICS_CHAT_MAX_ATTEMPTS", "3")
    store = FileConversationStateStore(base_dir=tmp_path / "chat-state")
    store.upsert_pending_event("slack", "alice", "C1", _event())
    ran: list[str] = []
    _install_runner(monkeypatch, ran, fail_events={"E1"})
    abandoned: list[dict] = []
    monkeypatch.setattr(
        "guildbotics.drivers.pending_chat_dispatcher.record_chat_dispatch_abandoned",
        lambda **kwargs: abandoned.append(kwargs),
    )
    context = _FakeContext()
    context.logger.error = lambda *a, **k: None
    person = Person(person_id="alice", name="A", is_active=True)
    dispatcher = PendingChatDispatcher(
        context,  # type: ignore[arg-type]
        state_store=store,
        execution_coordinator=TaskRunCoordinator(),
    )

    for _ in range(3):
        assert await dispatcher.process_person(person) == 0
        pending = store.load_pending_events("slack", "alice", "C1")
        if not pending:
            break
        # Only the backoff wait is skipped; everything else is the real path.
        pending[0].next_attempt_at = "2000-01-01T00:00:00+00:00"
        store.save_pending_event("slack", "alice", "C1", pending[0])

    assert ran == ["E1", "E1", "E1"]
    assert [event["attempt_count"] for event in abandoned] == [3]
    assert store.is_processed_event("slack", "alice", "C1", "E1")
    assert store.load_pending_events("slack", "alice", "C1") == []
