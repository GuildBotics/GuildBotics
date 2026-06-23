from __future__ import annotations

import hashlib
import logging
import types
import uuid

import pytest

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.drivers.event_listener_runner import (
    ChatBackfillPolicy,
    EventListenerRunner,
    SlackConnectionKey,
)
from guildbotics.entities.task import Task
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_service import (
    ChatEvent,
    ChatEventPage,
    ChatIdentity,
    ChatPostResult,
)
from guildbotics.integrations.chat_state_store import ThreadMessageState
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.integrations.slack.slack_chat_service import SlackApiError
from guildbotics.observability import correlation_fields
from guildbotics.runtime.context import Context
from guildbotics.runtime.event_listener import (
    INCOMING_CHAT_EVENT_KEY,
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


async def _no_backfill(*args, **kwargs):
    return 0


class _WorkflowChatService:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str | None]] = []
        self.reactions: list[tuple[str, str, str]] = []

    async def get_bot_identity(self) -> ChatIdentity:
        return ChatIdentity(user_id="U_ALICE", display_name="AliceBot")

    async def resolve_channel_id(self, channel_name: str) -> str | None:
        return None

    async def post_message(
        self, channel_id: str, text: str, *, thread_ts: str | None = None
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
async def test_dispatch_incoming_event_sets_shared_state_and_runs_workflow(monkeypatch):
    captured = {}

    class _FakeCommandRunner:
        def __init__(self, context, command_name, command_args):
            captured["context"] = context
            captured["command_name"] = command_name
            captured["command_args"] = list(command_args)

        async def run(self):
            captured["correlation"] = correlation_fields()
            return "ok"

    monkeypatch.setattr(
        "guildbotics.drivers.event_listener_runner.CommandRunner",
        _FakeCommandRunner,
    )

    base = _FakeContext()
    runner = EventListenerRunner(base)  # type: ignore[arg-type]
    person = types.SimpleNamespace(person_id="alice")
    item = IncomingChatEvent(
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

    out = await runner.dispatch_incoming_event(person, item)

    assert out == "ok"
    assert captured["command_name"] == "workflows/chat_conversation_workflow"
    assert captured["command_args"] == []
    assert captured["correlation"]["source"] == "event_listener"
    assert captured["correlation"]["attributes"]["event.provider"] == "slack"
    assert captured["correlation"]["attributes"]["slack.channel"] == "C1"
    ctx = captured["context"]
    payload = ctx.shared_state[INCOMING_CHAT_EVENT_KEY]
    assert payload["service_name"] == "slack"
    assert payload["channel_id"] == "C1"
    assert payload["event"]["event_id"] == "E1"
    assert ctx.closed is True


@pytest.mark.asyncio
async def test_dispatch_incoming_event_runs_real_workflow_via_command_runner(
    monkeypatch, tmp_path
):
    """Dispatch runs the real chat workflow through CommandRunner.

    The workflow no longer posts to Slack directly; it delegates to the
    ``functions/handle_chat_event`` CLI agent, which records run evidence and
    completion. Here that agent invocation is faked at the CommandRunner boundary
    (the real agent is an external process), so the test verifies the wiring and
    the evidence-driven state update rather than a direct chat post.
    """
    import guildbotics.drivers.command_runner as command_runner_module
    from guildbotics.integrations.file_chat_state_store import (
        FileConversationStateStore,
    )

    # The workflow uses the default RunStore / state store, both keyed off
    # GUILDBOTICS_DATA_DIR, so point them at a temp dir instead of user storage.
    monkeypatch.setenv("GUILDBOTICS_DATA_DIR", str(tmp_path / "data"))

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
        task=Task(title="t", description="d"),
        message="",
    )
    runner = EventListenerRunner(context)

    # Fake the agent invocation at the CommandRunner boundary: record a reply
    # evidence + completion exactly as `guildbotics member chat ...` would.
    async def fake_invoke(self, name, *args, **kwargs):
        if name != "functions/handle_chat_event":
            return None
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
    incoming = IncomingChatEvent(
        service_name="slack",
        channel_id="C1",
        event=ChatEvent(
            event_id=event_id,
            channel_id="C1",
            message_ts=thread_ts,
            thread_ts=thread_ts,
            author_id="U_USER",
            text="<@U_ALICE> integration path",
            mentions=["U_ALICE"],
        ),
    )

    await runner.dispatch_incoming_event(person, incoming)

    # The workflow delegates instead of posting to Slack directly.
    assert chat_service.posts == []
    # Evidence drives the processed-event record and the appended bot reply.
    # Read through the default store (same GUILDBOTICS_DATA_DIR-based path the
    # workflow wrote to).
    state_store = FileConversationStateStore()
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert event_id in channel_state.processed_event_ids
    thread_messages = state_store.load_thread_messages(
        "slack", "alice", "C1", thread_ts
    )
    bot_messages = [message for message in thread_messages if message.is_bot_message]
    assert [message.message_ts for message in bot_messages] == ["200.1"]


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
        def __init__(self, *, logger, app_token, base_url=None, person_ids=None):
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
        def __init__(self, *, logger, app_token, base_url=None, person_ids=None):
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
async def test_run_once_dispatches_events_and_marks_processed(monkeypatch, tmp_path):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        profile={"chat": {"subscriptions": [{"service": "slack", "channel_id": "C1"}]}},
    )
    ctx = _FakeContext()
    ctx.team = types.SimpleNamespace(members=[alice])
    runner = EventListenerRunner(
        ctx, state_store=FileConversationStateStore(base_dir=tmp_path)
    )  # type: ignore[arg-type]

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

    dispatched = []

    async def _fake_dispatch(person, item):
        dispatched.append((person.person_id, item.event.event_id, item.channel_id))
        return "ok"

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
    monkeypatch.setattr(runner, "_get_or_create_listener", lambda key: fake_listener)
    monkeypatch.setattr(runner, "dispatch_incoming_event", _fake_dispatch)
    mark_calls = []
    monkeypatch.setattr(
        runner,
        "_mark_processed_for_person",
        lambda person, incoming: mark_calls.append(
            (person.person_id, incoming.event.event_id)
        ),
    )
    monkeypatch.setattr(
        runner, "_is_processed_for_person", lambda person, incoming: False
    )
    monkeypatch.setattr(runner, "_backfill_due_events", _no_backfill)

    await runner._run_once()

    assert dispatched == [("alice", "E1", "C1")]
    assert mark_calls == [("alice", "E1")]
    assert fake_listener.started == 1


@pytest.mark.asyncio
async def test_run_once_broadcasts_same_event_to_multiple_people(monkeypatch, tmp_path):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        profile={"chat": {"subscriptions": [{"service": "slack", "channel_id": "C1"}]}},
    )
    bob = Person(
        person_id="bob",
        name="Bob",
        is_active=True,
        profile={"chat": {"subscriptions": [{"service": "slack", "channel_id": "C1"}]}},
    )
    ctx = _FakeContext()
    ctx.team = types.SimpleNamespace(members=[alice, bob])
    runner = EventListenerRunner(
        ctx, state_store=FileConversationStateStore(base_dir=tmp_path)
    )  # type: ignore[arg-type]

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

    dispatched = []
    marked = []

    async def _fake_dispatch(person, item):
        dispatched.append(person.person_id)
        return "ok"

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
    monkeypatch.setattr(runner, "_get_or_create_listener", lambda key: _FakeListener())
    monkeypatch.setattr(runner, "dispatch_incoming_event", _fake_dispatch)
    monkeypatch.setattr(
        runner, "_is_processed_for_person", lambda person, incoming: False
    )
    monkeypatch.setattr(
        runner,
        "_mark_processed_for_person",
        lambda person, incoming: marked.append(
            (person.person_id, incoming.event.event_id)
        ),
    )
    monkeypatch.setattr(runner, "_backfill_due_events", _no_backfill)

    await runner._run_once()

    assert dispatched == ["alice", "bob"]
    assert marked == [("alice", "E1"), ("bob", "E1")]


@pytest.mark.asyncio
async def test_run_once_skips_person_when_already_processed(monkeypatch, tmp_path):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        profile={"chat": {"subscriptions": [{"service": "slack", "channel_id": "C1"}]}},
    )
    bob = Person(
        person_id="bob",
        name="Bob",
        is_active=True,
        profile={"chat": {"subscriptions": [{"service": "slack", "channel_id": "C1"}]}},
    )
    ctx = _FakeContext()
    ctx.team = types.SimpleNamespace(members=[alice, bob])
    runner = EventListenerRunner(
        ctx, state_store=FileConversationStateStore(base_dir=tmp_path)
    )  # type: ignore[arg-type]

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

    dispatched = []

    async def _fake_dispatch(person, item):
        dispatched.append(person.person_id)
        return "ok"

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
    monkeypatch.setattr(runner, "_get_or_create_listener", lambda key: _FakeListener())
    monkeypatch.setattr(runner, "dispatch_incoming_event", _fake_dispatch)
    monkeypatch.setattr(
        runner,
        "_is_processed_for_person",
        lambda person, incoming: person.person_id == "bob",
    )
    monkeypatch.setattr(
        runner, "_mark_processed_for_person", lambda person, incoming: None
    )
    monkeypatch.setattr(runner, "_backfill_due_events", _no_backfill)

    await runner._run_once()

    assert dispatched == ["alice"]


@pytest.mark.asyncio
async def test_run_once_backfills_channel_and_known_thread_events(
    monkeypatch, tmp_path
):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        profile={"chat": {"subscriptions": [{"service": "slack", "channel_id": "C1"}]}},
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

    dispatched = []

    async def _fake_dispatch(person, item):
        dispatched.append((person.person_id, item.event.event_id))
        return "ok"

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
    monkeypatch.setattr(runner, "_get_or_create_listener", lambda key: _FakeListener())
    monkeypatch.setattr(runner, "dispatch_incoming_event", _fake_dispatch)

    await runner._run_once()
    await runner._run_once()

    assert dispatched == [
        ("alice", "C1:101.1"),
        ("alice", "C1:102.1"),
    ]
    assert chat_service.channel_calls == 1
    assert chat_service.thread_calls == [("C1", "100.1")]
    cursor = state_store.load_channel_cursor("slack", "alice", "C1")
    assert cursor.oldest_ts == "101.1"
    assert state_store.load_pending_events("slack", "alice", "C1") == []


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
async def test_run_once_uses_connection_service_for_backfill_and_pending_dispatch(
    monkeypatch,
):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        profile={"chat": {"subscriptions": [{"service": "slack", "channel_id": "C1"}]}},
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
    observed_dispatch_services = []

    async def _fake_backfill(person, service_name, channel_id, policy):
        observed_backfill_services.append(service_name)
        return 0

    async def _fake_dispatch(person, service_name, channel_id):
        observed_dispatch_services.append(service_name)
        return 0, 0

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
    monkeypatch.setattr(runner, "_get_or_create_listener", lambda key: _FakeListener())
    monkeypatch.setattr(runner, "_backfill_due_events", _fake_backfill)
    monkeypatch.setattr(runner, "_dispatch_pending_events", _fake_dispatch)

    await runner._run_once()

    assert observed_backfill_services == ["slack-compatible"]
    assert observed_dispatch_services == ["slack-compatible"]


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
        profile={
            "chat": {
                "subscriptions": [
                    {
                        "service": "slack",
                        "channel_name": "dev-chat",
                        "event_source": "socket_mode",
                    }
                ]
            }
        },
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
        profile={"chat": {"subscriptions": [{"service": "slack", "channel_id": "C1"}]}},
    )
    bob = Person(
        person_id="bob",
        name="Bob",
        is_active=True,
        profile={"chat": {"subscriptions": [{"service": "slack", "channel_id": "C2"}]}},
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
