from __future__ import annotations

import asyncio
import json
import threading
import time
import types
from collections.abc import Callable

import pytest
from click.testing import CliRunner

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.drivers.event_listener_runner import (
    ChatBackfillPolicy,
    EventListenerRunner,
)
from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.chat_receive_status import ChatReceiveStatus
from guildbotics.integrations.chat_state_store import (
    ChannelCursorState,
    ThreadConversationState,
)
from guildbotics.integrations.event_listener import EventListener
from guildbotics.integrations.factory import ServiceIntegrationFactory
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.integrations.local.chat import LocalChatService
from guildbotics.runtime.chat_service import ChatEvent
from guildbotics.runtime.workflow_invocation import WORKFLOW_INVOCATION_KEY
from tests.guildbotics.local_chat import at, chat_event, chat_team, lines, say
from tests.guildbotics.runtime.configured_team import make_context
from tests.guildbotics.runtime.test_context import DummyBrainFactory

WARNING_ARG_COUNT = 2


def _member(person_id: str, *channels: dict, **fields) -> Person:
    """An active member subscribed to ``channels`` (``chat`` settings each,
    with ``name``)."""
    return Person(
        person_id=person_id,
        name=person_id.title(),
        is_active=True,
        message_channels=[
            {"name": channel.pop("name", "dev-chat"), "chat": channel}
            for channel in (dict(c) for c in channels)
        ],
        **fields,
    )


class _Context:
    """A runner's context: the team, and each member's own chat."""

    def __init__(self, team: Team) -> None:
        self.team = team
        self.infos: list[tuple] = []
        self.warnings: list[tuple] = []
        self.logger = types.SimpleNamespace(
            info=lambda *a, **k: self.infos.append(a),
            warning=lambda *a, **k: self.warnings.append(a),
            debug=lambda *a, **k: None,
        )
        self.closed = False

    def clone_for(self, person: Person):
        async def aclose() -> None:
            return None

        return types.SimpleNamespace(
            person=person,
            get_chat_service=lambda: LocalChatService(person),
            aclose=aclose,
        )

    async def aclose(self) -> None:
        self.closed = True


def _runner(*members: Person, **kwargs) -> tuple[EventListenerRunner, _Context]:
    context = _Context(chat_team(*members))
    return EventListenerRunner(context, **kwargs), context  # type: ignore[arg-type]


class _Listener(EventListener):
    """A connection a test hands its events to."""

    def __init__(self) -> None:
        self.events: list[ChatEvent] = []
        self.started = 0
        self.stopped = 0
        self.failed = False
        self.live = True

    @property
    def connected(self) -> bool:
        return self.live

    @property
    def auth_failed(self) -> bool:
        return self.failed

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1
        self.live = False

    def drain_events(self) -> list[ChatEvent]:
        drained, self.events = self.events, []
        return drained


def _listen(monkeypatch, runner: EventListenerRunner) -> list[tuple]:
    """Have the runner's connections be :class:`_Listener` s; the calls that
    made them, as ``(listener, members)``."""
    made: list[tuple] = []

    def create(logger, team, persons, on_activity):
        listener = _Listener()
        made.append((listener, [person.person_id for person in persons]))
        return listener

    monkeypatch.setattr(runner._factory, "create_event_listener", create)
    return made


async def _no_backfill(*args, **kwargs):
    return 0


def test_runner_notifies_when_worker_thread_stops(monkeypatch) -> None:
    stopped = threading.Event()
    runner, _ = _runner(on_stopped=stopped.set)

    async def finish_immediately() -> None:
        return

    monkeypatch.setattr(runner, "_run_loop", finish_immediately)

    runner.start()
    assert stopped.wait(timeout=1.0)


def _wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


@pytest.mark.asyncio
@pytest.mark.usefixtures("commands_in_process", "configured_team")
async def test_a_message_goes_round_to_the_members_reply_on_the_local_chat(
    monkeypatch,
):
    """Receive, dispatch, turn, ``member chat reply``, ``member chat complete``:
    the whole chat workflow on the reference provider.

    Only the AI CLI tool is a stand-in, at the command runner: it runs the
    member's chat commands as the turn would.
    """
    import guildbotics.commands.runner as command_runner_module
    from guildbotics.cli import member as member_module
    from guildbotics.drivers.pending_chat_dispatcher import PendingChatDispatcher
    from guildbotics.runtime.member_invocation import (
        MemberInvocation,
        member_invocation_scope,
    )
    from guildbotics.runtime.person_lease import current_person_lease

    alice = _member("alice", {"channel_id": "C1"})
    team = chat_team(alice)
    team.project.language = "en"
    context = make_context(team, ServiceIntegrationFactory(), DummyBrainFactory())
    say("C1", "earlier talk", message_id="m0", occurred_at=at(0))

    # Receive: the runner listens on the local chat, where a person writes.
    runner = EventListenerRunner(
        context, poll_interval_seconds=0.05, startup_backfill_minutes=0
    )
    runner.start()
    try:
        _wait_for(lambda: ChatReceiveStatus().available("local", "alice", "C1"))
        asked = say("C1", "@alice please summarize", message_id="m1")
        store = FileConversationStateStore()
        _wait_for(lambda: store.load_pending_events("local", "alice", "C1") != [])
    finally:
        runner.stop()
        runner.join(timeout=5)
    # The status the member's publication checks stays as the listener left
    # it; it is the listener's to say while it runs.
    ChatReceiveStatus().save("local", "alice", "C1", state="ready")

    turns: list[dict] = []

    async def run_member_commands(self, name, *args, **kwargs):
        """The turn: read the updates, reply, and complete, as the member."""
        if name != "functions/handle_chat_event":
            return None
        turns.append(kwargs)
        invocation = self.context.shared_state[WORKFLOW_INVOCATION_KEY]
        # The turn writes under the lease the dispatcher took for the member.
        lease = current_person_lease()
        assert lease is not None and lease.person_id == "alice"
        monkeypatch.setattr(
            member_module,
            "resolve_member_context",
            lambda _person: (context.clone_for(alice), alice),
        )
        with member_invocation_scope(
            MemberInvocation(
                run_id=invocation.run_id, work=invocation.work, lease=lease
            )
        ):
            for arguments, content in (
                (["updates"], None),
                (
                    [
                        "reply",
                        "--channel-id",
                        kwargs["channel_id"],
                        "--thread-id",
                        kwargs["thread_id"],
                    ],
                    "Here is the summary.",
                ),
                (["complete", "--status", "done"], "Replied with a summary."),
            ):
                # A member command runs on a thread of its own, as the
                # broker runs it.
                result = await asyncio.to_thread(
                    CliRunner().invoke,
                    member_module.member,
                    ["chat", *arguments, "--person", "alice"]
                    + (["--content-stdin"] if content else []),
                    input=content,
                )
                assert result.exit_code == 0, result.output
        return {"status": "done", "message": "done"}

    monkeypatch.setattr(
        command_runner_module.CommandRunner, "_invoke", run_member_commands
    )
    dispatcher = PendingChatDispatcher(context)

    assert await dispatcher.process_person(alice) == 1

    assert [json.loads(t["unprocessed_messages"])[0]["content"] for t in turns] == [
        "@alice please summarize"
    ]
    replies = [line for line in lines("C1") if line["author"] == "alice"]
    assert [(r["text"], r["thread_id"]) for r in replies] == [
        ("Here is the summary.", asked.message_id)
    ]
    assert store.is_processed_event("local", "alice", "C1", asked.event_id)
    assert store.load_pending_events("local", "alice", "C1") == []
    (record,) = RunStore().records()
    assert RunStore().status(record.run_id).status == "done"


@pytest.mark.asyncio
async def test_members_of_one_connection_share_one_listener(monkeypatch):
    alice = _member("alice", {"channel_id": "C1"})
    bob = _member("bob", {"channel_id": "C2"})
    runner, _ = _runner(alice, bob)
    made = _listen(monkeypatch, runner)
    monkeypatch.setattr(runner, "_backfill_due_events", _no_backfill)

    await runner._run_once()
    await runner._run_once()

    assert [members for _, members in made] == [["alice", "bob"]]
    assert made[0][0].started == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_no_chat_service_means_nothing_to_listen_to(monkeypatch):
    alice = _member("alice", {"channel_id": "C1"})
    context = _Context(Team(project=Project(name="demo"), members=[alice]))
    runner = EventListenerRunner(context)  # type: ignore[arg-type]
    made = _listen(monkeypatch, runner)

    await runner._run_once()

    assert made == []
    assert await runner._build_person_subscriptions_by_connection() == {}


@pytest.mark.asyncio
async def test_a_member_who_cannot_connect_is_skipped(monkeypatch):
    alice = _member("alice", {"channel_id": "C1"})
    bob = _member("bob", {"channel_id": "C2"})
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "xapp-alice")
    context = _Context(
        Team(
            project=Project(services={"chat_service": {"name": "slack"}}),
            members=[alice, bob],
        )
    )
    runner = EventListenerRunner(context)  # type: ignore[arg-type]

    grouped = await runner._build_person_subscriptions_by_connection()

    assert [[p.person_id for p, _ in group] for group in grouped.values()] == [
        ["alice"]
    ]
    assert any(
        "skipped person=%s" in str(args[0])
        and len(args) >= WARNING_ARG_COUNT
        and args[1] == "bob"
        for args in context.warnings
    )


@pytest.mark.asyncio
async def test_received_events_are_queued_with_their_channels_participation(
    monkeypatch,
):
    alice = _member("alice", {"channel_id": "C1", "participation": "social"})
    runner, _ = _runner(alice)
    made = _listen(monkeypatch, runner)
    monkeypatch.setattr(runner, "_backfill_due_events", _no_backfill)
    await runner._run_once()
    made[0][0].events.append(chat_event("100.1", text="hello"))

    await runner._run_once()

    # The listener only queues the event (with its channel participation); the
    # member worker runs the workflow and marks it processed later.
    store = runner._state_store
    pending = store.load_pending_events("local", "alice", "C1")
    assert [pe.event.event_id for pe in pending] == ["C1:100.1"]
    assert pending[0].chat_participation == "social"
    assert not store.is_processed_event("local", "alice", "C1", "C1:100.1")


@pytest.mark.asyncio
async def test_a_workflow_status_message_is_marked_processed_not_queued(monkeypatch):
    alice = _member("alice", {"channel_id": "C1"})
    runner, _ = _runner(alice)
    made = _listen(monkeypatch, runner)
    monkeypatch.setattr(runner, "_backfill_due_events", _no_backfill)
    await runner._run_once()
    made[0][0].events.append(
        chat_event(
            "100.1",
            text="workflow notice",
            metadata={
                "event_type": "guildbotics.workflow_status",
                "event_payload": {"routing": "suppress"},
            },
        )
    )

    await runner._run_once()

    store = runner._state_store
    assert store.load_pending_events("local", "alice", "C1") == []
    assert store.is_processed_event("local", "alice", "C1", "C1:100.1") is True


@pytest.mark.asyncio
async def test_an_event_is_queued_for_each_member_who_has_not_handled_it(
    monkeypatch,
):
    alice = _member("alice", {"channel_id": "C1"})
    bob = _member("bob", {"channel_id": "C1"})
    carol = _member("carol", {"channel_id": "C1"})
    runner, _ = _runner(alice, bob, carol)
    # Carol already handled this event in a previous pass.
    runner._state_store.mark_processed_event("local", "carol", "C1", "C1:100.1")
    made = _listen(monkeypatch, runner)
    monkeypatch.setattr(runner, "_backfill_due_events", _no_backfill)
    await runner._run_once()
    made[0][0].events.append(chat_event("100.1", text="hello"))

    await runner._run_once()

    def queued(person_id: str) -> list[str]:
        return [
            pe.event.event_id
            for pe in runner._state_store.load_pending_events("local", person_id, "C1")
        ]

    assert (queued("alice"), queued("bob"), queued("carol")) == (
        ["C1:100.1"],
        ["C1:100.1"],
        [],
    )


@pytest.mark.asyncio
async def test_backfill_reads_the_channel_and_known_threads_once_per_interval(
    monkeypatch,
):
    # Startup backfill reaches back far enough for the messages written here.
    alice = _member(
        "alice", {"channel_id": "C1", "startup_backfill_minutes": 60 * 24 * 365}
    )
    runner, _ = _runner(alice)
    _listen(monkeypatch, runner)
    say("C1", "@alice startup backfill", message_id="t2", occurred_at=at(0))
    say("C1", "root", message_id="t1", occurred_at=at(0))
    say("C1", "follow-up", message_id="r1", thread_id="t1", occurred_at=at(1))
    runner._state_store.save_thread_state(
        "local", "alice", "C1", "t1", ThreadConversationState("C1", "t1")
    )
    lists: list[str] = []
    original = LocalChatService.list_thread_events

    async def counting(self, channel_id, **kwargs):
        lists.append(kwargs["thread_id"])
        return await original(self, channel_id, **kwargs)

    monkeypatch.setattr(LocalChatService, "list_thread_events", counting)

    await runner._run_once()
    await runner._run_once()

    assert lists == ["t1"]
    pending = runner._state_store.load_pending_events("local", "alice", "C1")
    # Backfilled channel and thread messages land in the pending queue (the
    # member worker runs them later); nothing is dispatched here.
    assert [pe.event.event_id for pe in pending] == ["C1:t1", "C1:t2", "C1:r1"]
    cursor = runner._state_store.load_channel_cursor("local", "alice", "C1")
    assert cursor.watermark == at(0)


@pytest.mark.asyncio
async def test_backfill_receive_cutoff_floors_the_window_and_filters_events():
    alice = Person(person_id="alice", name="Alice", is_active=True)
    runner, _ = _runner(alice)
    runner._service = "local"
    store = runner._state_store
    # A watermark whose overlap window (205 - 60 = 145) reaches before the reset
    # cutoff; the cutoff must clamp the window and drop what precedes it.
    store.save_channel_cursor(
        "local", "alice", "C1", ChannelCursorState(watermark=at(205))
    )
    store.save_receive_cutoff("local", "alice", at(200))
    say("C1", "long before", message_id="a", occurred_at=at(100))
    say("C1", "at the cutoff", message_id="b", occurred_at=at(200))
    say("C1", "after cutoff", message_id="c", occurred_at=at(250))
    windows: list = []
    service = LocalChatService(alice)
    original = service.list_channel_events

    async def recording(channel_id, **kwargs):
        windows.append(kwargs["since"])
        return await original(channel_id, **kwargs)

    service.list_channel_events = recording  # type: ignore[method-assign]

    count = await runner._backfill_channel_events(
        alice,
        "C1",
        service,
        ChatBackfillPolicy(),
        store.load_receive_cutoff("local", "alice"),
    )

    assert windows == [at(200)]
    assert count == 1
    pending = store.load_pending_events("local", "alice", "C1")
    assert [pe.event.event_id for pe in pending] == ["C1:c"]
    assert store.load_channel_cursor("local", "alice", "C1").watermark == at(250)


@pytest.mark.asyncio
async def test_messages_of_one_time_are_both_backfilled_in_the_order_of_their_ids():
    """A chat may name two messages with one time. The window includes both,
    whichever the chat lists first, the queue orders them by id, and the
    watermark rests on their time so the next window reads them again rather
    than skipping one."""
    alice = Person(person_id="alice", name="Alice", is_active=True)
    runner, _ = _runner(alice)
    runner._service = "local"
    store = runner._state_store
    store.save_channel_cursor(
        "local", "alice", "C1", ChannelCursorState(watermark=at(100))
    )
    say("C1", "second by id", message_id="m-b", occurred_at=at(100))
    say("C1", "first by id", message_id="m-a", occurred_at=at(100))
    policy = ChatBackfillPolicy(overlap_seconds=0)

    count = await runner._backfill_channel_events(
        alice, "C1", LocalChatService(alice), policy, None
    )

    assert count == 2  # noqa: PLR2004
    pending = store.load_pending_events("local", "alice", "C1")
    assert [pe.event.message_id for pe in pending] == ["m-a", "m-b"]
    assert store.load_channel_cursor("local", "alice", "C1").watermark == at(100)
    # Read again from the same watermark, neither is queued a second time.
    again = await runner._backfill_channel_events(
        alice, "C1", LocalChatService(alice), policy, None
    )
    assert again == 2  # noqa: PLR2004
    assert len(store.load_pending_events("local", "alice", "C1")) == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_thread_backfill_reads_from_the_newest_known_message_less_overlap(
    monkeypatch,
):
    alice = Person(person_id="alice", name="Alice", is_active=True)
    runner, _ = _runner(alice)
    runner._service = "local"
    store = runner._state_store
    say("C1", "root", message_id="t1", occurred_at=at(0))
    say("C1", "old", message_id="r1", thread_id="t1", occurred_at=at(10))
    say("C1", "new", message_id="r2", thread_id="t1", occurred_at=at(100))
    store.append_thread_message(
        "local",
        "alice",
        "C1",
        "t1",
        chat_selection_message("r2", at(80)),
    )

    count = await runner._backfill_thread_events(
        alice,
        "C1",
        "t1",
        LocalChatService(alice),
        ChatBackfillPolicy(overlap_seconds=60),
        None,
    )

    assert count == 1
    pending = store.load_pending_events("local", "alice", "C1")
    assert [pe.event.message_id for pe in pending] == ["r2"]


def chat_selection_message(message_id: str, occurred_at):
    from guildbotics.integrations.chat_state_store import ThreadMessageState

    return ThreadMessageState(
        channel_id="C1",
        thread_id="t1",
        message_id=message_id,
        occurred_at=occurred_at,
        author_id="otota",
        text="known",
    )


@pytest.mark.asyncio
async def test_a_thread_that_is_gone_is_not_backfilled_again(monkeypatch):
    alice = Person(person_id="alice", name="Alice", is_active=True)
    runner, _ = _runner(alice)
    runner._service = "local"
    store = runner._state_store
    say("C1", "elsewhere", message_id="x", occurred_at=at(0))
    store.save_thread_state(
        "local", "alice", "C1", "gone", ThreadConversationState("C1", "gone")
    )
    lists: list[str] = []
    original = LocalChatService.list_thread_events

    async def counting(self, channel_id, **kwargs):
        lists.append(kwargs["thread_id"])
        return await original(self, channel_id, **kwargs)

    monkeypatch.setattr(LocalChatService, "list_thread_events", counting)

    for _ in range(2):
        await runner._backfill_channel_and_threads(
            alice, "C1", ChatBackfillPolicy(startup_minutes=0)
        )

    state = store.load_thread_state("local", "alice", "C1", "gone")
    assert state.backfill_disabled_reason == "thread_not_found"
    assert state.backfill_error_count == 1
    assert state.last_backfill_error == "thread_not_found"
    assert lists == ["gone"]


@pytest.mark.asyncio
async def test_backfill_is_due_once_per_member_channel_and_interval(monkeypatch):
    runner, _ = _runner()
    runner._service = "local"
    calls = []

    async def _fake_backfill(person, channel_id, policy):
        calls.append((person.person_id, channel_id))
        return 1

    monkeypatch.setattr(runner, "_backfill_channel_and_threads", _fake_backfill)
    alice = types.SimpleNamespace(person_id="alice")
    bob = types.SimpleNamespace(person_id="bob")

    policy = ChatBackfillPolicy(startup_minutes=60, interval_seconds=300.0)
    assert await runner._backfill_due_events(alice, "C1", policy) == 1
    assert await runner._backfill_due_events(bob, "C1", policy) == 1
    assert await runner._backfill_due_events(alice, "C1", policy) == 0
    assert calls == [("alice", "C1"), ("bob", "C1")]


@pytest.mark.asyncio
async def test_backfill_failure_updates_attempt_cadence(monkeypatch):
    runner, _ = _runner()
    runner._service = "local"
    calls = {"count": 0}

    async def _fake_backfill(person, channel_id, policy):
        calls["count"] += 1
        raise RuntimeError("transient")

    monkeypatch.setattr(runner, "_backfill_channel_and_threads", _fake_backfill)
    person = types.SimpleNamespace(person_id="alice")

    policy = ChatBackfillPolicy(startup_minutes=60, interval_seconds=0.0)
    assert await runner._backfill_due_events(person, "C1", policy) == 0
    assert await runner._backfill_due_events(person, "C1", policy) == 0
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_a_channel_name_is_resolved_once_while_the_subscription_stands(
    monkeypatch,
):
    say("dev-chat", "hello", message_id="m0")
    alice = _member("alice", {"channel_name": "dev-chat"})
    runner, _ = _runner(alice)
    resolved: list[str] = []
    original = LocalChatService.resolve_channel_id

    async def counting(self, channel_name):
        resolved.append(channel_name)
        return await original(self, channel_name)

    monkeypatch.setattr(LocalChatService, "resolve_channel_id", counting)

    grouped1 = await runner._build_person_subscriptions_by_connection()
    grouped2 = await runner._build_person_subscriptions_by_connection()

    assert [list(subs) for _, subs in next(iter(grouped1.values()))] == [["dev-chat"]]
    assert grouped2 == grouped1
    assert resolved == ["dev-chat"]


def test_subscription_signature_normalizes_participation_defaults():
    runner, _ = _runner()
    base = {"channel_id": "C1"}

    assert runner._subscription_signature(
        [{**base, "participation": None}]
    ) == runner._subscription_signature([{**base, "participation": "  "}])
    assert runner._subscription_signature(
        [{**base, "participation": "unknown"}]
    ) == runner._subscription_signature([{**base, "participation": "strict"}])
    assert runner._subscription_signature(
        [{**base, "participation": "social"}]
    ) != runner._subscription_signature([{**base, "participation": "strict"}])


@pytest.mark.asyncio
async def test_aclose_sources_stops_listeners_and_clears_caches():
    runner, _ = _runner()
    listener = _Listener()
    runner._listeners["key"] = listener
    runner._subscription_channel_cache["alice"] = ((), {"C1": ChatBackfillPolicy()})
    runner._last_group_log_state = (1, 1)

    await runner._aclose_sources()

    assert listener.stopped == 1
    assert runner._listeners == {}
    assert runner._subscription_channel_cache == {}
    assert runner._last_group_log_state is None


def test_get_status_summary_surfaces_auth_failed_connections():
    runner, _ = _runner()
    failed, ok = _Listener(), _Listener()
    failed.failed = True
    runner._listeners = {"failed": failed, "ok": ok}
    yuki = Person(person_id="yuki", name="Yuki")
    aiko = Person(person_id="aiko", name="Aiko")
    runner._connection_subscriptions = {
        "failed": [(yuki, {"C1": ChatBackfillPolicy()})],
        "ok": [(aiko, {"C1": ChatBackfillPolicy()})],
    }

    summary = runner.get_status_summary()

    assert summary["events_auth_failed_count"] == 1
    assert summary["events_auth_failed_persons"] == ["yuki"]


@pytest.mark.asyncio
async def test_stop_cancels_in_flight_cycle(monkeypatch):
    runner, _ = _runner()
    # Pretend we are running inside the worker loop so stop() can schedule the
    # cancellation onto it.
    runner._loop = asyncio.get_running_loop()

    started = asyncio.Event()

    async def _long_run_once():
        # Simulate a backfill awaiting a slow provider request that the stop
        # event cannot interrupt on its own.
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(runner, "_run_once", _long_run_once)

    loop_task = asyncio.create_task(runner._run_loop())
    await asyncio.wait_for(started.wait(), timeout=1.0)

    runner.stop()

    # Cancellation aborts the in-flight cycle so the runner exits promptly instead
    # of waiting out the request and overshooting the stop timeout.
    await asyncio.wait_for(loop_task, timeout=1.0)
    assert runner._stop_event.is_set()
    assert runner._cycle_failure_count == 0


def _connected(
    runner: EventListenerRunner, person: Person, listener: EventListener
) -> str:
    """``listener`` as the runner's connection for ``person`` on ``C1``."""
    runner._service = "local"
    runner._connection_subscriptions["key"] = [(person, {"C1": ChatBackfillPolicy()})]
    runner._listeners["key"] = listener
    return "key"


@pytest.mark.asyncio
async def test_socket_notification_persists_while_backfill_is_waiting(monkeypatch):
    runner, _ = _runner(poll_interval_seconds=30)
    person = Person(person_id="alice", name="Alice")
    listener = _Listener()
    grouped = {"key": [(person, {"C1": ChatBackfillPolicy()})]}
    runner._service = "local"
    runner._listeners["key"] = listener
    runner._loop = asyncio.get_running_loop()
    backfill_started = asyncio.Event()
    saved = asyncio.Event()

    async def subscriptions():
        return grouped

    async def slow_backfill(*args):
        backfill_started.set()
        await asyncio.Event().wait()
        return 0

    original_upsert = runner._state_store.upsert_pending_event

    def upsert(*args):
        original_upsert(*args)
        saved.set()

    monkeypatch.setattr(
        runner, "_build_person_subscriptions_by_connection", subscriptions
    )
    monkeypatch.setattr(runner, "_backfill_person", slow_backfill)
    monkeypatch.setattr(runner._state_store, "upsert_pending_event", upsert)
    task = asyncio.create_task(runner._run_loop())
    try:
        await asyncio.wait_for(backfill_started.wait(), 1)
        listener.events.append(
            chat_event("101.1", thread_id="100.1", text="cancel", author_id="U2")
        )
        # The listener notifies from another thread.
        await asyncio.to_thread(runner._wake_receiver)
        await asyncio.wait_for(saved.wait(), 1)
        assert (
            runner._state_store.load_pending_events("local", "alice", "C1")[
                0
            ].event.text
            == "cancel"
        )
        assert ChatReceiveStatus().available("local", "alice", "C1")
    finally:
        runner.stop()
        await asyncio.wait_for(task, 1)
    assert not ChatReceiveStatus().available("local", "alice", "C1")


def test_receive_save_failure_retains_event_and_marks_unavailable(monkeypatch):
    runner, _ = _runner()
    listener = _Listener()
    key = _connected(runner, Person(person_id="alice", name="Alice"), listener)
    listener.events.append(
        chat_event("101.1", thread_id="100.1", text="cancel", author_id="U2")
    )
    original = runner._state_store.upsert_pending_event

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(runner._state_store, "upsert_pending_event", fail)
    runner._flush_listener(key)
    assert not ChatReceiveStatus().available("local", "alice", "C1")
    monkeypatch.setattr(runner._state_store, "upsert_pending_event", original)
    runner._flush_listener(key)
    assert ChatReceiveStatus().available("local", "alice", "C1")
    assert len(runner._state_store.load_pending_events("local", "alice", "C1")) == 1


def test_receiver_can_restart_on_another_event_loop(monkeypatch):
    runner, _ = _runner()

    async def one_cycle():
        # Let the receiver bind its wait to this loop before ending the cycle.
        await asyncio.sleep(0.001)
        runner._stop_event.set()

    monkeypatch.setattr(runner, "_backfill_loop", one_cycle)
    for _ in range(2):
        runner._stop_event.clear()
        asyncio.run(runner._run_loop())


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["socket", "backfill"])
async def test_sync_lock_wait_keeps_heartbeat_alive_and_prevents_publication(
    monkeypatch, source
):
    from guildbotics.capabilities.chat_updates import (
        ChatUpdatesRequired,
        check_chat_updates,
        ensure_chat_current,
    )
    from guildbotics.integrations import chat_receive_status
    from guildbotics.runtime.member_invocation import (
        ChatSubject,
        MemberInvocation,
        Work,
        member_invocation_scope,
    )
    from guildbotics.utils.shared_write_lock import shared_write_lock

    scope = ("local", "alice", "C1")
    RunStore().append_evidence("run", "chat_batch", {"event_ids": ["C1:100.1"]})
    invocation = MemberInvocation(
        run_id="run",
        work=Work.of_chat(ChatSubject("local", "C1", "100.1", "C1:100.1", "alice")),
    )
    now = [100.0]
    monkeypatch.setattr(
        chat_receive_status,
        "time",
        types.SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0]),
    )
    runner, _ = _runner()
    listener = _Listener()
    key = _connected(runner, Person(person_id="alice", name="Alice"), listener)
    message = chat_event(
        "103.1", thread_id="100.1", author_id="U_USER", text="cancel deployment"
    )
    if source == "socket":
        listener.events.append(message)
    runner._receive_status.save(*scope, state="ready")
    locked, release = threading.Event(), threading.Event()

    def hold_sync_lock():
        with shared_write_lock():
            locked.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold_sync_lock)
    thread.start()
    task = None
    try:
        assert await asyncio.to_thread(locked.wait, 1)
        if source == "backfill":
            task = asyncio.create_task(
                runner._write_backfill(
                    scope,
                    lambda: runner._state_store.upsert_pending_event(*scope, message),
                )
            )
            await asyncio.sleep(0.01)
        runner._flush_listener(key)  # Must not block on the real shared lock.
        for elapsed in (5, 10, 20, 35):
            now[0] = 100.0 + elapsed
            runner._flush_listener(key)
            assert ChatReceiveStatus().state(*scope) == "catching_up"
            with member_invocation_scope(invocation):
                assert check_chat_updates("alice")["status"] == "catching_up"
                with pytest.raises(ChatUpdatesRequired):
                    ensure_chat_current("alice")
        assert not runner._state_store.load_pending_events(*scope)
    finally:
        release.set()
        await asyncio.to_thread(thread.join, 1)
        if task:
            await asyncio.wait_for(task, 2)
    runner._flush_listener(key)
    assert ChatReceiveStatus().state(*scope) == "ready"
    with member_invocation_scope(invocation):
        result = check_chat_updates("alice")
        assert [item["event_id"] for item in result["messages"]] == ["C1:103.1"]
        ensure_chat_current("alice")


def test_wake_receiver_uses_one_loop_reference_during_shutdown():
    class StoppingRunner(EventListenerRunner):
        @property
        def _loop(self):
            loop = self.current_loop
            self.current_loop = None
            return loop

        @_loop.setter
        def _loop(self, value):
            self.current_loop = value

    runner = StoppingRunner(_Context(chat_team()))  # type: ignore[arg-type]
    calls = []
    runner._loop = types.SimpleNamespace(
        call_soon_threadsafe=lambda callback: calls.append(callback)
    )
    runner._wake_receiver()
    assert calls == [runner._receive_wakeup.set]
    runner._wake_receiver()  # Shutdown has now cleared the loop.
    assert len(calls) == 1
