from __future__ import annotations

import hashlib
import json
import logging
import threading
import types
import uuid

import pytest

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.drivers.event_listener_runner import (
    ChatBackfillPolicy,
    EventListenerRunner,
    SlackConnectionKey,
)
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_service import (
    ChatEvent,
    ChatEventPage,
    ChatIdentity,
    ChatPostResult,
)
from guildbotics.integrations.chat_state_store import (
    ChannelCursorState,
    ThreadMessageState,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.integrations.slack.slack_chat_service import SlackApiError
from guildbotics.runtime.context import Context
from guildbotics.runtime.event_listener import (
    IncomingChatEvent,
)
from guildbotics.runtime.integration_factory import IntegrationFactory
from tests.guildbotics.runtime.test_context import (
    DummyBrainFactory,
    DummyLoaderFactory,
    _make_team,
)

EXPECTED_LISTENER_COUNT = 2
WARNING_ARG_COUNT = 2
DEFAULT_BACKFILL_LIMIT = 100
EXPECTED_BACKFILL_ATTEMPTS = 2


class _FakeContext:
    def __init__(self) -> None:
        self.closed = False
        self.last_clone = None
        self.infos: list[tuple] = []
        self.warnings: list[tuple] = []
        self.logger = types.SimpleNamespace(
            info=lambda *a, **k: self.infos.append(a),
            warning=lambda *a, **k: self.warnings.append(a),
        )
        self.team = types.SimpleNamespace(members=[])

    def clone_for(self, person):
        clone = types.SimpleNamespace(
            person=person,
            shared_state={},
            pipe="",
            closed=False,
        )

        async def _aclose():
            clone.closed = True

        clone.aclose = _aclose
        self.last_clone = clone
        return clone

    async def aclose(self):
        self.closed = True


def test_runner_notifies_when_worker_thread_stops(monkeypatch) -> None:
    stopped = threading.Event()
    runner = EventListenerRunner(  # type: ignore[arg-type]
        _FakeContext(), on_stopped=stopped.set
    )

    async def finish_immediately() -> None:
        return

    monkeypatch.setattr(runner, "_run_loop", finish_immediately)

    runner.start()
    assert stopped.wait(timeout=1.0)


async def _no_backfill(*args, **kwargs):
    return 0


class _WorkflowChatService:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str | None]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.events: list[ChatEvent] = []

    async def list_thread_events(
        self, channel_id, *, thread_ts, cursor=None, limit=100
    ):
        return ChatEventPage(events=self.events)

    async def get_bot_identity(self) -> ChatIdentity:
        return ChatIdentity(user_id="U_ALICE", display_name="AliceBot")

    async def resolve_channel_id(self, channel_name: str) -> str | None:
        return None

    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ChatPostResult:
        self.posts.append((channel_id, text, thread_ts))
        ts = "999.1"
        return ChatPostResult(
            channel_id=channel_id, message_ts=ts, thread_ts=thread_ts or ts
        )

    async def add_reaction(
        self, channel_id: str, message_ts: str, reaction: str
    ) -> None:
        self.reactions.append((channel_id, message_ts, reaction))

    def normalize_participant_text(
        self, text: str, participant_labels: dict[str, str]
    ) -> str:
        for user_id, label in participant_labels.items():
            text = text.replace(f"<@{user_id}>", f"@{label}")
        return text

    def render_participant_text(
        self, text: str, participant_labels: dict[str, str]
    ) -> str:
        for user_id, label in participant_labels.items():
            text = text.replace(f"@{label}", f"<@{user_id}>")
        return text

    async def aclose(self) -> None:
        return None


class _WorkflowIntegrationFactory(IntegrationFactory):
    def __init__(self, chat_service: _WorkflowChatService) -> None:
        self.chat_service = chat_service

    def create_ticket_manager(self, logger, person, team):
        raise AssertionError("unused in this test")

    def create_chat_service(self, logger, person, team):
        return self.chat_service


class _BackfillChatService:
    def __init__(self) -> None:
        self.channel_calls = 0
        self.thread_calls: list[tuple[str, str]] = []
        self.thread_error: SlackApiError | None = None

    async def list_channel_events(
        self,
        channel_id: str,
        *,
        cursor: str | None = None,
        oldest_ts: str | None = None,
        latest_ts: str | None = None,
        limit: int = 100,
    ) -> ChatEventPage:
        self.channel_calls += 1
        assert channel_id == "C1"
        assert cursor is None
        assert oldest_ts is not None
        assert latest_ts is None
        assert limit == DEFAULT_BACKFILL_LIMIT
        return ChatEventPage(
            events=[
                ChatEvent(
                    event_id="C1:101.1",
                    channel_id="C1",
                    message_ts="101.1",
                    thread_ts="101.1",
                    author_id="U_USER",
                    text="<@U_ALICE> startup backfill",
                    mentions=["U_ALICE"],
                )
            ],
            oldest_ts="101.1",
        )

    async def list_thread_events(
        self,
        channel_id: str,
        *,
        thread_ts: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ChatEventPage:
        self.thread_calls.append((channel_id, thread_ts))
        if self.thread_error is not None:
            raise self.thread_error
        assert cursor is None
        assert limit == DEFAULT_BACKFILL_LIMIT
        return ChatEventPage(
            events=[
                ChatEvent(
                    event_id="C1:102.1",
                    channel_id="C1",
                    message_ts="102.1",
                    thread_ts="100.1",
                    author_id="U_USER",
                    text="follow-up",
                    mentions=[],
                    is_thread_reply=True,
                )
            ],
            oldest_ts="102.1",
        )


class _BackfillContext(_FakeContext):
    def __init__(self, chat_service: _BackfillChatService) -> None:
        super().__init__()
        self.chat_service = chat_service

    def clone_for(self, person):
        clone = super().clone_for(person)
        clone.get_chat_service = lambda: self.chat_service
        return clone


@pytest.mark.asyncio
async def test_pending_dispatcher_runs_real_workflow_via_command_runner(
    monkeypatch, tmp_path
):
    """The member worker's dispatcher runs the real chat workflow end to end.

    The workflow delegates to the ``functions/handle_chat_event`` AI CLI tool,
    which records run evidence and completion. That agent invocation is faked at
    the CommandRunner boundary (the real agent is an external process), so the
    test verifies the wiring and the evidence-driven state update.
    """
    import guildbotics.drivers.command_runner as command_runner_module
    from guildbotics.drivers.pending_chat_dispatcher import PendingChatDispatcher
    from guildbotics.integrations.file_chat_state_store import (
        FileConversationStateStore,
    )

    # The workflow uses the default RunStore / state store, both keyed off
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))

    chat_service = _WorkflowChatService()
    team = _make_team(language="en")
    person = Person(person_id="alice", name="Alice", is_active=True)
    team.members = [person]
    context = Context(
        loader_factory=DummyLoaderFactory(team),
        integration_factory=_WorkflowIntegrationFactory(chat_service),
        brain_factory=DummyBrainFactory(),
        logger=logging.getLogger("test-event-runner"),
        person=person,
        message="",
    )

    # Fake the agent invocation at the CommandRunner boundary: record a reply
    # evidence + completion exactly as `guildbotics member chat ...` would.
    agent_inputs = []

    async def fake_invoke(self, name, *args, **kwargs):
        if name != "functions/handle_chat_event":
            return None
        agent_inputs.append(json.loads(kwargs["unprocessed_messages"]))
        run_id = kwargs["workflow_run_id"]
        store = RunStore()
        store.append_evidence(
            run_id,
            "chat_reply",
            {
                "service": "slack",
                "channel_id": kwargs["channel_id"],
                "message_ts": "200.1",
                "thread_ts": kwargs["thread_ts"],
                "text": "Acknowledged.",
                "posted": True,
            },
        )
        store.complete_run(
            run_id,
            "done",
            "Posted a reply.",
            subject_type="chat",
            subject_id=(
                f"slack:{kwargs['channel_id']}:"
                f"{kwargs['thread_ts']}:{kwargs['event_id']}"
            ),
            person_id=kwargs["person_id"],
        )
        return {"status": "done", "message": "done"}

    monkeypatch.setattr(command_runner_module.CommandRunner, "_invoke", fake_invoke)

    unique_suffix = uuid.uuid4().hex
    event_id = f"E_INT_{unique_suffix}"
    thread_ts = f"100.{abs(hash(unique_suffix)) % 100000}"

    state_store = FileConversationStateStore()
    state_store.upsert_pending_event(
        "slack",
        "alice",
        "C1",
        ChatEvent(
            event_id=event_id,
            channel_id="C1",
            message_ts=thread_ts,
            thread_ts=thread_ts,
            author_id="U_USER",
            text="<@U_ALICE> integration path",
            mentions=["U_ALICE"],
        ),
    )

    followup = ChatEvent(
        event_id=f"{event_id}_correction",
        channel_id="C1",
        thread_ts=thread_ts,
        message_ts="101.1",
        author_id="U_USER",
        text="Correction: use the updated requirement",
        mentions=["U_ALICE"],
    )
    chat_service.events = [followup]
    state_store.upsert_pending_event("slack", "alice", "C1", followup)
    dispatcher = PendingChatDispatcher(context, state_store=state_store)
    processed = await dispatcher.process_person(person)

    assert processed == 1
    assert len(agent_inputs) == 1
    assert [item["timestamp"] for item in agent_inputs[0]] == [thread_ts, "101.1"]
    assert state_store.is_processed_event("slack", "alice", "C1", followup.event_id)
    assert state_store.load_pending_events("slack", "alice", "C1") == []
    # The workflow delegates instead of posting to Slack directly.
    assert chat_service.posts == []
    # Evidence drives the processed-event record and the appended bot reply.
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert event_id in channel_state.processed_event_ids
    thread_messages = state_store.load_thread_messages(
        "slack", "alice", "C1", thread_ts
    )
    bot_messages = [message for message in thread_messages if message.is_bot_message]
    assert [message.message_ts for message in bot_messages] == ["200.1"]
    # The event is removed from the queue once processed.
    assert state_store.load_pending_events("slack", "alice", "C1") == []


def test_make_connection_key_hashes_app_token(monkeypatch):
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "xapp-secret-1")
    person = Person(person_id="alice", name="Alice")
    ctx = _FakeContext()
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]

    key, app_token = runner._make_connection_key(person)

    assert key.service == "slack"
    assert key.event_source == "socket_mode"
    assert key.base_url == "https://slack.com/api"
    assert key.app_token_hash == hashlib.sha256(b"xapp-secret-1").hexdigest()
    assert app_token == "xapp-secret-1"
    assert "xapp-secret-1" not in repr(key)


def test_get_or_create_listener_reuses_same_connection_key(monkeypatch):
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "xapp-shared")
    monkeypatch.setenv("BOB_SLACK_APP_TOKEN", "xapp-shared")
    created: list[tuple[str, str | None]] = []

    class _FakeSlackSocketEventListener:
        def __init__(
            self, *, logger, app_token, base_url=None, person_ids=None, on_activity=None
        ):
            created.append((app_token, base_url))

        def start(self):
            return None

        def stop(self):
            return None

        def drain_events(self):
            return []

    monkeypatch.setattr(
        "guildbotics.drivers.event_listener_runner.SlackSocketEventListener",
        _FakeSlackSocketEventListener,
    )

    ctx = _FakeContext()
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]
    alice = Person(person_id="alice", name="Alice")
    bob = Person(person_id="bob", name="Bob")

    key1, token1 = runner._make_connection_key(alice)
    runner._listener_tokens[key1] = token1
    key2, token2 = runner._make_connection_key(bob)
    runner._listener_tokens[key2] = token2
    s1 = runner._get_or_create_listener(key1)
    s2 = runner._get_or_create_listener(key2)

    assert s1 is s2
    assert len(created) == 1


def test_get_or_create_listener_splits_when_base_url_differs(monkeypatch):
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "xapp-shared")
    monkeypatch.setenv("BOB_SLACK_APP_TOKEN", "xapp-shared")
    created: list[tuple[str, str | None]] = []

    class _FakeSlackSocketEventListener:
        def __init__(
            self, *, logger, app_token, base_url=None, person_ids=None, on_activity=None
        ):
            created.append((app_token, base_url))

        def start(self):
            return None

        def stop(self):
            return None

        def drain_events(self):
            return []

    monkeypatch.setattr(
        "guildbotics.drivers.event_listener_runner.SlackSocketEventListener",
        _FakeSlackSocketEventListener,
    )

    ctx = _FakeContext()
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]
    alice = Person(person_id="alice", name="Alice", profile={"chat": {}})
    bob = Person(
        person_id="bob",
        name="Bob",
        profile={"chat": {"slack_base_url": "https://proxy.example.test/slack"}},
    )

    key1, token1 = runner._make_connection_key(alice)
    runner._listener_tokens[key1] = token1
    key2, token2 = runner._make_connection_key(bob)
    runner._listener_tokens[key2] = token2
    s1 = runner._get_or_create_listener(key1)
    s2 = runner._get_or_create_listener(key2)

    assert s1 is not s2
    assert len(created) == EXPECTED_LISTENER_COUNT


@pytest.mark.asyncio
async def test_run_once_queues_drained_events(monkeypatch, tmp_path):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        message_channels=[
            {
                "service": "slack",
                "name": "dev-chat",
                "chat": {"channel_id": "C1", "participation": "social"},
            }
        ],
    )
    ctx = _FakeContext()
    ctx.team = types.SimpleNamespace(members=[alice])
    store = FileConversationStateStore(base_dir=tmp_path)
    runner = EventListenerRunner(ctx, state_store=store)  # type: ignore[arg-type]

    class _FakeListener:
        def __init__(self):
            self.started = 0

        def start(self):
            self.started += 1

        def stop(self):
            return None

        def drain_events(self):
            return [
                IncomingChatEvent(
                    service_name="slack",
                    channel_id="C1",
                    event=ChatEvent(
                        event_id="E1",
                        channel_id="C1",
                        message_ts="100.1",
                        thread_ts="100.1",
                        author_id="U1",
                        text="hello",
                    ),
                )
            ]

    async def _fake_resolve(person, subscriptions):
        return subscriptions

    monkeypatch.setattr(runner, "_resolve_subscription_channels", _fake_resolve)
    monkeypatch.setattr(
        runner,
        "_make_connection_key",
        lambda person: (
            SlackConnectionKey(
                service="slack",
                event_source="socket_mode",
                app_token_hash="h" * 64,
                base_url="https://slack.example/api",
            ),
            "xapp",
        ),
    )
    fake_listener = _FakeListener()
    monkeypatch.setattr(
        runner,
        "_get_or_create_listener",
        lambda key: runner._listeners.setdefault(key, fake_listener),
    )
    monkeypatch.setattr(runner, "_backfill_due_events", _no_backfill)

    await runner._run_once()

    # The listener only queues the event (with its channel participation); the
    # member worker runs the workflow and marks it processed later.
    pending = store.load_pending_events("slack", "alice", "C1")
    assert [pe.event.event_id for pe in pending] == ["E1"]
    assert pending[0].chat_participation == "social"
    assert not store.is_processed_event("slack", "alice", "C1", "E1")
    assert fake_listener.started == 1


@pytest.mark.asyncio
async def test_run_once_suppresses_workflow_status_metadata(monkeypatch, tmp_path):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        message_channels=[
            {"service": "slack", "name": "dev-chat", "chat": {"channel_id": "C1"}}
        ],
    )
    ctx = _FakeContext()
    ctx.team = types.SimpleNamespace(members=[alice])
    store = FileConversationStateStore(base_dir=tmp_path)
    runner = EventListenerRunner(ctx, state_store=store)  # type: ignore[arg-type]

    class _FakeListener:
        def start(self):
            return None

        def stop(self):
            return None

        def drain_events(self):
            return [
                IncomingChatEvent(
                    service_name="slack",
                    channel_id="C1",
                    event=ChatEvent(
                        event_id="E1",
                        channel_id="C1",
                        message_ts="100.1",
                        thread_ts="100.1",
                        author_id="U1",
                        text="workflow notice",
                        metadata={
                            "event_type": "guildbotics.workflow_status",
                            "event_payload": {"routing": "suppress"},
                        },
                    ),
                )
            ]

    async def _fake_resolve(person, subscriptions):
        return subscriptions

    monkeypatch.setattr(runner, "_resolve_subscription_channels", _fake_resolve)
    monkeypatch.setattr(
        runner,
        "_make_connection_key",
        lambda person: (
            SlackConnectionKey(
                service="slack",
                event_source="socket_mode",
                app_token_hash="h" * 64,
                base_url="https://slack.example/api",
            ),
            "xapp",
        ),
    )
    monkeypatch.setattr(
        runner,
        "_get_or_create_listener",
        lambda key: runner._listeners.setdefault(key, _FakeListener()),
    )
    monkeypatch.setattr(runner, "_backfill_due_events", _no_backfill)

    await runner._run_once()

    assert store.load_pending_events("slack", "alice", "C1") == []
    assert store.is_processed_event("slack", "alice", "C1", "E1") is True


@pytest.mark.asyncio
async def test_run_once_queues_event_for_multiple_people(monkeypatch, tmp_path):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        message_channels=[
            {"service": "slack", "name": "dev-chat", "chat": {"channel_id": "C1"}}
        ],
    )
    bob = Person(
        person_id="bob",
        name="Bob",
        is_active=True,
        message_channels=[
            {"service": "slack", "name": "dev-chat", "chat": {"channel_id": "C1"}}
        ],
    )
    ctx = _FakeContext()
    ctx.team = types.SimpleNamespace(members=[alice, bob])
    store = FileConversationStateStore(base_dir=tmp_path)
    runner = EventListenerRunner(ctx, state_store=store)  # type: ignore[arg-type]

    class _FakeListener:
        def start(self):
            return None

        def stop(self):
            return None

        def drain_events(self):
            return [
                IncomingChatEvent(
                    service_name="slack",
                    channel_id="C1",
                    event=ChatEvent(
                        event_id="E1",
                        channel_id="C1",
                        message_ts="100.1",
                        thread_ts="100.1",
                        author_id="U1",
                        text="hello",
                    ),
                )
            ]

    async def _fake_resolve(person, subscriptions):
        return subscriptions

    monkeypatch.setattr(runner, "_resolve_subscription_channels", _fake_resolve)
    # Different people but same shared key.
    monkeypatch.setattr(
        runner,
        "_make_connection_key",
        lambda person: (
            SlackConnectionKey(
                service="slack",
                event_source="socket_mode",
                app_token_hash="h" * 64,
                base_url="https://slack.example/api",
            ),
            "xapp",
        ),
    )
    monkeypatch.setattr(
        runner,
        "_get_or_create_listener",
        lambda key: runner._listeners.setdefault(key, _FakeListener()),
    )
    monkeypatch.setattr(runner, "_backfill_due_events", _no_backfill)

    await runner._run_once()

    # The event is queued for each subscribed member.
    assert [
        pe.event.event_id for pe in store.load_pending_events("slack", "alice", "C1")
    ] == ["E1"]
    assert [
        pe.event.event_id for pe in store.load_pending_events("slack", "bob", "C1")
    ] == ["E1"]


@pytest.mark.asyncio
async def test_run_once_does_not_queue_already_processed(monkeypatch, tmp_path):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        message_channels=[
            {"service": "slack", "name": "dev-chat", "chat": {"channel_id": "C1"}}
        ],
    )
    bob = Person(
        person_id="bob",
        name="Bob",
        is_active=True,
        message_channels=[
            {"service": "slack", "name": "dev-chat", "chat": {"channel_id": "C1"}}
        ],
    )
    ctx = _FakeContext()
    ctx.team = types.SimpleNamespace(members=[alice, bob])
    store = FileConversationStateStore(base_dir=tmp_path)
    # Bob already handled this event in a previous pass.
    store.mark_processed_event("slack", "bob", "C1", "E1")
    runner = EventListenerRunner(ctx, state_store=store)  # type: ignore[arg-type]

    class _FakeListener:
        def start(self):
            return None

        def stop(self):
            return None

        def drain_events(self):
            return [
                IncomingChatEvent(
                    service_name="slack",
                    channel_id="C1",
                    event=ChatEvent(
                        event_id="E1",
                        channel_id="C1",
                        message_ts="100.1",
                        thread_ts="100.1",
                        author_id="U1",
                        text="hello",
                    ),
                )
            ]

    async def _fake_resolve(person, subscriptions):
        return subscriptions

    monkeypatch.setattr(runner, "_resolve_subscription_channels", _fake_resolve)
    monkeypatch.setattr(
        runner,
        "_make_connection_key",
        lambda person: (
            SlackConnectionKey(
                service="slack",
                event_source="socket_mode",
                app_token_hash="h" * 64,
                base_url="https://slack.example/api",
            ),
            "xapp",
        ),
    )
    monkeypatch.setattr(
        runner,
        "_get_or_create_listener",
        lambda key: runner._listeners.setdefault(key, _FakeListener()),
    )
    monkeypatch.setattr(runner, "_backfill_due_events", _no_backfill)

    await runner._run_once()

    # Queued for alice but not for bob (already processed).
    assert [
        pe.event.event_id for pe in store.load_pending_events("slack", "alice", "C1")
    ] == ["E1"]
    assert store.load_pending_events("slack", "bob", "C1") == []


@pytest.mark.asyncio
async def test_run_once_backfills_channel_and_known_thread_events(
    monkeypatch, tmp_path
):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        message_channels=[
            {"service": "slack", "name": "dev-chat", "chat": {"channel_id": "C1"}}
        ],
    )
    chat_service = _BackfillChatService()
    ctx = _BackfillContext(chat_service)
    ctx.team = types.SimpleNamespace(members=[alice])
    state_store = FileConversationStateStore(base_dir=tmp_path)
    state_store.append_thread_message(
        "slack",
        "alice",
        "C1",
        "100.1",
        ThreadMessageState(
            channel_id="C1",
            thread_ts="100.1",
            message_ts="100.1",
            author_id="U_USER",
            text="<@U_ALICE> please check",
            mentions=["U_ALICE"],
        ),
    )
    runner = EventListenerRunner(ctx, state_store=state_store)  # type: ignore[arg-type]

    class _FakeListener:
        def start(self):
            return None

        def stop(self):
            return None

        def drain_events(self):
            return []

    async def _fake_resolve(person, subscriptions):
        return subscriptions

    monkeypatch.setattr(runner, "_resolve_subscription_channels", _fake_resolve)
    monkeypatch.setattr(
        runner,
        "_make_connection_key",
        lambda person: (
            SlackConnectionKey(
                service="slack",
                event_source="socket_mode",
                app_token_hash="h" * 64,
                base_url="https://slack.example/api",
            ),
            "xapp",
        ),
    )
    monkeypatch.setattr(
        runner,
        "_get_or_create_listener",
        lambda key: runner._listeners.setdefault(key, _FakeListener()),
    )

    await runner._run_once()
    await runner._run_once()

    assert chat_service.channel_calls == 1
    assert chat_service.thread_calls == [("C1", "100.1")]
    cursor = state_store.load_channel_cursor("slack", "alice", "C1")
    assert cursor.oldest_ts == "101.1"
    # Backfilled channel + thread events land in the pending queue (the member
    # worker runs them later); they are not dispatched here.
    pending = state_store.load_pending_events("slack", "alice", "C1")
    assert sorted(pe.event.event_id for pe in pending) == ["C1:101.1", "C1:102.1"]


@pytest.mark.asyncio
async def test_backfill_receive_cutoff_floors_oldest_and_filters_events(tmp_path):
    alice = Person(person_id="alice", name="Alice", is_active=True)
    ctx = _BackfillContext(_BackfillChatService())
    ctx.team = types.SimpleNamespace(members=[alice])
    store = FileConversationStateStore(base_dir=tmp_path)
    # An existing watermark whose overlap window (205 - 60 = 145) reaches before
    # the reset cutoff; the cutoff must clamp the query and drop pre-cutoff events.
    store.save_channel_cursor(
        "slack", "alice", "C1", ChannelCursorState(oldest_ts="205.0")
    )
    store.save_receive_cutoff("slack", "alice", "200.0")
    runner = EventListenerRunner(ctx, state_store=store)  # type: ignore[arg-type]

    seen: dict[str, str | None] = {}

    class _CutoffChatService:
        async def list_channel_events(
            self,
            channel_id: str,
            *,
            cursor: str | None = None,
            oldest_ts: str | None = None,
            latest_ts: str | None = None,
            limit: int = 100,
        ) -> ChatEventPage:
            seen["oldest_ts"] = oldest_ts
            return ChatEventPage(
                events=[
                    ChatEvent(
                        event_id="C1:150.0",
                        channel_id="C1",
                        message_ts="150.0",
                        thread_ts="150.0",
                        author_id="U",
                        text="before cutoff",
                    ),
                    ChatEvent(
                        event_id="C1:250.0",
                        channel_id="C1",
                        message_ts="250.0",
                        thread_ts="250.0",
                        author_id="U",
                        text="after cutoff",
                    ),
                ],
                oldest_ts="250.0",
            )

    count = await runner._backfill_channel_events(
        alice, "slack", "C1", _CutoffChatService(), ChatBackfillPolicy()
    )

    # The query is floored at the cutoff instead of watermark-minus-overlap.
    assert seen["oldest_ts"] == "200.0"
    # Only the post-cutoff event is queued; the pre-cutoff one is filtered out.
    assert count == 1
    pending = store.load_pending_events("slack", "alice", "C1")
    assert [pe.event.event_id for pe in pending] == ["C1:250.0"]


@pytest.mark.asyncio
async def test_thread_not_found_disables_future_thread_backfill(tmp_path):
    alice = Person(person_id="alice", name="Alice", is_active=True)
    chat_service = _BackfillChatService()
    chat_service.thread_error = SlackApiError(
        "conversations.replies", "thread_not_found"
    )
    ctx = _BackfillContext(chat_service)
    state_store = FileConversationStateStore(base_dir=tmp_path)
    state_store.append_thread_message(
        "slack",
        "alice",
        "C1",
        "100.1",
        ThreadMessageState(
            channel_id="C1",
            thread_ts="100.1",
            message_ts="100.1",
            author_id="U_USER",
            text="<@U_ALICE> stale thread",
            mentions=["U_ALICE"],
        ),
    )
    runner = EventListenerRunner(ctx, state_store=state_store)  # type: ignore[arg-type]

    await runner._backfill_channel_and_threads(
        alice, "slack", "C1", ChatBackfillPolicy()
    )
    await runner._backfill_channel_and_threads(
        alice, "slack", "C1", ChatBackfillPolicy()
    )

    state = state_store.load_thread_state("slack", "alice", "C1", "100.1")
    assert state.backfill_disabled_reason == "thread_not_found"
    assert state.backfill_error_count == 1
    assert state.last_backfill_error == "thread_not_found"
    assert chat_service.thread_calls == [("C1", "100.1")]


@pytest.mark.asyncio
async def test_run_once_uses_connection_service_for_backfill(monkeypatch):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        message_channels=[
            {"service": "slack", "name": "dev-chat", "chat": {"channel_id": "C1"}}
        ],
    )
    ctx = _FakeContext()
    ctx.team = types.SimpleNamespace(members=[alice])
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]

    class _FakeListener:
        def start(self):
            return None

        def stop(self):
            return None

        def drain_events(self):
            return []

    async def _fake_resolve(person, subscriptions):
        return subscriptions

    observed_backfill_services = []

    async def _fake_backfill(person, service_name, channel_id, policy):
        observed_backfill_services.append(service_name)
        return 0

    monkeypatch.setattr(runner, "_resolve_subscription_channels", _fake_resolve)
    monkeypatch.setattr(
        runner,
        "_make_connection_key",
        lambda person: (
            SlackConnectionKey(
                service="slack-compatible",
                event_source="socket_mode",
                app_token_hash="h" * 64,
                base_url="https://slack.example/api",
            ),
            "xapp",
        ),
    )
    monkeypatch.setattr(
        runner,
        "_get_or_create_listener",
        lambda key: runner._listeners.setdefault(key, _FakeListener()),
    )
    monkeypatch.setattr(runner, "_backfill_due_events", _fake_backfill)

    await runner._run_once()

    assert observed_backfill_services == ["slack-compatible"]


@pytest.mark.asyncio
async def test_backfill_tracking_is_scoped_by_service_name(monkeypatch):
    ctx = _FakeContext()
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]
    person = types.SimpleNamespace(person_id="alice")
    calls = []

    async def _fake_backfill(person, service_name, channel_id, policy):
        calls.append((service_name, channel_id))
        return 1

    monkeypatch.setattr(runner, "_backfill_channel_and_threads", _fake_backfill)

    policy = ChatBackfillPolicy(startup_minutes=60, interval_seconds=300.0)
    assert await runner._backfill_due_events(person, "slack", "C1", policy) == 1
    assert await runner._backfill_due_events(person, "mattermost", "C1", policy) == 1
    assert await runner._backfill_due_events(person, "slack", "C1", policy) == 0
    assert calls == [("slack", "C1"), ("mattermost", "C1")]


@pytest.mark.asyncio
async def test_backfill_failure_updates_attempt_cadence(monkeypatch):
    ctx = _FakeContext()
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]
    person = types.SimpleNamespace(person_id="alice")
    calls = {"count": 0}

    async def _fake_backfill(person, service_name, channel_id, policy):
        calls["count"] += 1
        raise RuntimeError("transient")

    monkeypatch.setattr(runner, "_backfill_channel_and_threads", _fake_backfill)

    policy = ChatBackfillPolicy(startup_minutes=60, interval_seconds=0.0)
    assert await runner._backfill_due_events(person, "slack", "C1", policy) == 0
    assert await runner._backfill_due_events(person, "slack", "C1", policy) == 0
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_build_person_subscriptions_uses_cached_channel_resolution(monkeypatch):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        message_channels=[
            {
                "service": "slack",
                "name": "dev-chat",
                "chat": {"channel_name": "dev-chat", "event_source": "socket_mode"},
            }
        ],
    )
    ctx = _FakeContext()
    ctx.team = types.SimpleNamespace(members=[alice])
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]

    resolve_calls = {"count": 0}

    async def _fake_resolve(person, subscriptions):
        resolve_calls["count"] += 1
        return [{"service": "slack", "channel_id": "C1", "event_source": "socket_mode"}]

    monkeypatch.setattr(runner, "_resolve_subscription_channels", _fake_resolve)
    monkeypatch.setattr(
        runner,
        "_make_connection_key",
        lambda person: (
            SlackConnectionKey(
                service="slack",
                event_source="socket_mode",
                app_token_hash="h" * 64,
                base_url="https://slack.example/api",
            ),
            "xapp",
        ),
    )

    grouped1 = await runner._build_person_subscriptions_by_connection()
    grouped2 = await runner._build_person_subscriptions_by_connection()

    assert len(grouped1) == 1
    assert len(grouped2) == 1
    assert resolve_calls["count"] == 1


def test_subscription_signature_normalizes_participation_defaults():
    ctx = _FakeContext()
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]

    base_subscription = {
        "service": "slack",
        "channel_id": "C1",
        "event_source": "socket_mode",
    }

    assert runner._subscription_signature(
        [{**base_subscription, "participation": None}]
    ) == runner._subscription_signature([{**base_subscription, "participation": "  "}])
    assert runner._subscription_signature(
        [{**base_subscription, "participation": "unknown"}]
    ) == runner._subscription_signature(
        [{**base_subscription, "participation": "strict"}]
    )
    assert runner._subscription_signature(
        [{**base_subscription, "participation": "social"}]
    ) != runner._subscription_signature(
        [{**base_subscription, "participation": "strict"}]
    )


@pytest.mark.asyncio
async def test_aclose_sources_stops_listeners_and_clears_caches():
    ctx = _FakeContext()
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]
    stopped = []

    class _FakeListener:
        def __init__(self, name: str) -> None:
            self.name = name

        def start(self):
            return None

        def stop(self):
            stopped.append(self.name)

        def drain_events(self):
            return []

    key = SlackConnectionKey(
        service="slack",
        event_source="socket_mode",
        app_token_hash="h" * 64,
        base_url="https://slack.example/api",
    )
    runner._listeners[key] = _FakeListener("l1")
    runner._listener_tokens[key] = "xapp"
    runner._subscription_channel_cache["alice"] = ((), {"C1"})
    runner._last_group_log_state = (1, 1)

    await runner._aclose_sources()

    assert stopped == ["l1"]
    assert runner._listeners == {}
    assert runner._listener_tokens == {}
    assert runner._subscription_channel_cache == {}
    assert runner._last_group_log_state is None


def test_get_status_summary_surfaces_auth_failed_connections():
    ctx = _FakeContext()
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]

    class _AuthFailedListener:
        auth_failed = True

        def start(self):
            return None

        def stop(self):
            return None

        def drain_events(self):
            return []

    failed_key = SlackConnectionKey(
        service="slack",
        event_source="socket_mode",
        app_token_hash="f" * 64,
        base_url="https://slack.com/api",
    )
    ok_key = SlackConnectionKey(
        service="slack",
        event_source="socket_mode",
        app_token_hash="a" * 64,
        base_url="https://slack.com/api",
    )
    runner._listeners[failed_key] = _AuthFailedListener()
    runner._listeners[ok_key] = _AuthFailedListener.__new__(_AuthFailedListener)
    runner._listeners[ok_key].auth_failed = False  # type: ignore[attr-defined]
    runner._connection_person_ids = {
        failed_key: ["yuki", "yuki"],
        ok_key: ["aiko"],
    }

    summary = runner.get_status_summary()

    assert summary["events_auth_failed_count"] == 1
    assert summary["events_auth_failed_persons"] == ["yuki"]


@pytest.mark.asyncio
async def test_build_person_subscriptions_skips_person_with_missing_app_token(
    monkeypatch,
):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        message_channels=[
            {"service": "slack", "name": "dev-chat", "chat": {"channel_id": "C1"}}
        ],
    )
    bob = Person(
        person_id="bob",
        name="Bob",
        is_active=True,
        message_channels=[
            {"service": "slack", "name": "bob-chat", "chat": {"channel_id": "C2"}}
        ],
    )
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "xapp-alice")

    ctx = _FakeContext()
    ctx.team = types.SimpleNamespace(members=[alice, bob])
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]

    async def _fake_resolve(person, subscriptions):
        return subscriptions

    monkeypatch.setattr(runner, "_resolve_subscription_channels", _fake_resolve)

    grouped = await runner._build_person_subscriptions_by_connection()

    assert len(grouped) == 1
    only_group = next(iter(grouped.values()))
    assert [person.person_id for person, _channel_ids in only_group] == ["alice"]
    assert any(
        "skipped person=%s" in str(args[0])
        and len(args) >= WARNING_ARG_COUNT
        and args[1] == "bob"
        for args in ctx.warnings
    )


@pytest.mark.asyncio
async def test_stop_cancels_in_flight_cycle(monkeypatch):
    import asyncio

    ctx = _FakeContext()
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]
    # Pretend we are running inside the worker loop so stop() can schedule the
    # cancellation onto it.
    runner._loop = asyncio.get_running_loop()

    started = asyncio.Event()

    async def _long_run_once():
        # Simulate a backfill awaiting a slow Slack request that the stop event
        # cannot interrupt on its own.
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


@pytest.mark.asyncio
async def test_socket_notification_persists_while_backfill_is_waiting(monkeypatch):
    import asyncio

    from guildbotics.integrations.chat_receive_status import ChatReceiveStatus

    runner = EventListenerRunner(_FakeContext(), poll_interval_seconds=30)
    key = SlackConnectionKey("slack", "socket_mode", "hash", "https://slack.com/api")
    person = Person(person_id="alice", name="Alice")
    grouped = {key: [(person, {"C1": ChatBackfillPolicy()})]}
    backfill_started = asyncio.Event()
    saved = asyncio.Event()

    class Listener:
        connected = True
        events = []

        def start(self):
            pass

        def stop(self):
            self.connected = False

        def drain_events(self):
            result, self.events = self.events, []
            return result

    listener = Listener()
    runner._listeners[key] = listener
    runner._loop = asyncio.get_running_loop()

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
            IncomingChatEvent(
                "slack",
                "C1",
                ChatEvent(
                    event_id="E1",
                    channel_id="C1",
                    message_ts="101.1",
                    thread_ts="100.1",
                    text="cancel",
                    author_id="U_OTHER",
                ),
            )
        )
        # The socket reader notifies from another thread.
        await asyncio.to_thread(runner._wake_receiver)
        await asyncio.wait_for(saved.wait(), 1)
        assert (
            runner._state_store.load_pending_events("slack", "alice", "C1")[
                0
            ].event.text
            == "cancel"
        )
        assert ChatReceiveStatus().available("slack", "alice", "C1")
    finally:
        runner.stop()
        await asyncio.wait_for(task, 1)
    assert not ChatReceiveStatus().available("slack", "alice", "C1")


def test_receive_save_failure_retains_event_and_marks_unavailable(monkeypatch):
    from guildbotics.integrations.chat_receive_status import ChatReceiveStatus

    runner = EventListenerRunner(_FakeContext())
    key = SlackConnectionKey("slack", "socket_mode", "hash", "https://slack.com/api")
    runner._connection_subscriptions[key] = [
        (Person(person_id="alice", name="Alice"), {"C1": ChatBackfillPolicy()})
    ]
    events = [
        IncomingChatEvent(
            "slack",
            "C1",
            ChatEvent(
                event_id="E1",
                channel_id="C1",
                message_ts="101.1",
                thread_ts="100.1",
                text="cancel",
                author_id="U_OTHER",
            ),
        )
    ]

    def drain():
        result = events[:]
        events.clear()
        return result

    runner._listeners[key] = types.SimpleNamespace(connected=True, drain_events=drain)
    original = runner._state_store.upsert_pending_event

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(runner._state_store, "upsert_pending_event", fail)
    runner._flush_listener(key)
    assert not ChatReceiveStatus().available("slack", "alice", "C1")
    monkeypatch.setattr(runner._state_store, "upsert_pending_event", original)
    runner._flush_listener(key)
    assert ChatReceiveStatus().available("slack", "alice", "C1")
    assert len(runner._state_store.load_pending_events("slack", "alice", "C1")) == 1


def test_receiver_can_restart_on_another_event_loop(monkeypatch):
    import asyncio

    runner = EventListenerRunner(_FakeContext())

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
    import asyncio

    from guildbotics.capabilities.chat_updates import (
        ChatUpdatesRequired,
        check_chat_updates,
        ensure_chat_current,
    )
    from guildbotics.integrations import chat_receive_status
    from guildbotics.integrations.chat_receive_status import ChatReceiveStatus
    from guildbotics.runtime.member_invocation import (
        MemberInvocation,
        member_invocation_scope,
    )
    from guildbotics.utils.shared_write_lock import shared_write_lock

    scope = ("slack", "alice", "C1")
    RunStore().append_evidence(
        "run",
        "chat_batch",
        {
            "person_id": "alice",
            "service": "slack",
            "channel_id": "C1",
            "thread_ts": "100.1",
            "self_user_id": "U_BOT",
            "event_ids": ["E1"],
        },
    )
    invocation = MemberInvocation(run_id="run")
    now = [100.0]
    monkeypatch.setattr(
        chat_receive_status,
        "time",
        types.SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0]),
    )
    runner = EventListenerRunner(_FakeContext())
    key = SlackConnectionKey("slack", "socket_mode", "hash", "https://slack.com/api")
    runner._connection_subscriptions[key] = [
        (Person(person_id="alice", name="Alice"), {"C1": ChatBackfillPolicy()})
    ]
    message = ChatEvent(
        event_id="E3",
        channel_id="C1",
        message_ts="103.1",
        thread_ts="100.1",
        author_id="U_USER",
        text="cancel deployment",
    )
    events = [IncomingChatEvent("slack", "C1", message)] if source == "socket" else []

    def drain():
        result = events[:]
        events.clear()
        return result

    runner._listeners[key] = types.SimpleNamespace(connected=True, drain_events=drain)
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
                assert check_chat_updates("alice", "run")["status"] == "catching_up"
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
        result = check_chat_updates("alice", "run")
        assert [item["event_id"] for item in result["messages"]] == ["E3"]
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

    runner = StoppingRunner(_FakeContext())
    calls = []
    runner._loop = types.SimpleNamespace(
        call_soon_threadsafe=lambda callback: calls.append(callback)
    )
    runner._wake_receiver()
    assert calls == [runner._receive_wakeup.set]
    runner._wake_receiver()  # Shutdown has now cleared the loop.
    assert len(calls) == 1
