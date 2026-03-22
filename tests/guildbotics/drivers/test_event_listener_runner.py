from __future__ import annotations

import hashlib
import logging
import types
import uuid

import pytest

from guildbotics.drivers.event_listener_runner import EventListenerRunner, SlackConnectionKey
from guildbotics.entities.task import Task
from guildbotics.entities.team import Person
from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.chat_service import ChatIdentity, ChatPostResult
from guildbotics.runtime.context import Context
from guildbotics.runtime.event_listener import INCOMING_CHAT_EVENT_KEY, IncomingChatEvent
from tests.guildbotics.runtime.test_context import DummyBrainFactory, DummyLoaderFactory, _make_team
from guildbotics.runtime.integration_factory import IntegrationFactory


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
        return ChatPostResult(channel_id=channel_id, message_ts=ts, thread_ts=thread_ts or ts)

    async def add_reaction(self, channel_id: str, message_ts: str, reaction: str) -> None:
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

    def create_code_hosting_service(self, person, team, repository=None):
        raise AssertionError("unused in this test")

    def create_chat_service(self, logger, person, team):
        return self.chat_service


@pytest.mark.asyncio
async def test_dispatch_incoming_event_sets_shared_state_and_runs_workflow(monkeypatch):
    captured = {}

    class _FakeCommandRunner:
        def __init__(self, context, command_name, command_args):
            captured["context"] = context
            captured["command_name"] = command_name
            captured["command_args"] = list(command_args)

        async def run(self):
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
    ctx = captured["context"]
    payload = ctx.shared_state[INCOMING_CHAT_EVENT_KEY]
    assert payload["service_name"] == "slack"
    assert payload["channel_id"] == "C1"
    assert payload["event"]["event_id"] == "E1"
    assert ctx.closed is True


@pytest.mark.asyncio
async def test_dispatch_incoming_event_runs_real_workflow_via_command_runner(monkeypatch, tmp_path):
    from guildbotics.templates.commands.workflows import chat_conversation_workflow

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

    # Force workflow fallback path to use a temp state store rather than user storage.
    from guildbotics.integrations.file_chat_state_store import FileConversationStateStore

    monkeypatch.setattr(
        chat_conversation_workflow,
        "FileConversationStateStore",
        lambda: FileConversationStateStore(base_dir=tmp_path),
    )

    unique_suffix = uuid.uuid4().hex
    incoming = IncomingChatEvent(
        service_name="slack",
        channel_id="C1",
        event=ChatEvent(
            event_id=f"E_INT_{unique_suffix}",
            channel_id="C1",
            message_ts=f"100.{abs(hash(unique_suffix)) % 100000}",
            thread_ts=f"100.{abs(hash(unique_suffix)) % 100000}",
            author_id="U_USER",
            text="<@U_ALICE> integration path",
            mentions=["U_ALICE"],
        ),
    )

    await runner.dispatch_incoming_event(person, incoming)

    assert len(chat_service.posts) == 1
    channel_id, text, thread_ts = chat_service.posts[0]
    assert channel_id == "C1"
    assert thread_ts == incoming.event.thread_ts
    assert text.strip() != ""


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
        def __init__(self, *, logger, app_token, base_url=None):
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
        def __init__(self, *, logger, app_token, base_url=None):
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
    assert len(created) == 2


@pytest.mark.asyncio
async def test_run_once_dispatches_events_and_marks_processed(monkeypatch):
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
    monkeypatch.setattr(runner, "_mark_processed_for_person", lambda person, incoming: mark_calls.append((person.person_id, incoming.event.event_id)))
    monkeypatch.setattr(runner, "_is_processed_for_person", lambda person, incoming: False)

    await runner._run_once()

    assert dispatched == [("alice", "E1", "C1")]
    assert mark_calls == [("alice", "E1")]
    assert fake_listener.started == 1


@pytest.mark.asyncio
async def test_run_once_broadcasts_same_event_to_multiple_people(monkeypatch):
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
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]

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
    monkeypatch.setattr(runner, "_is_processed_for_person", lambda person, incoming: False)
    monkeypatch.setattr(
        runner,
        "_mark_processed_for_person",
        lambda person, incoming: marked.append((person.person_id, incoming.event.event_id)),
    )

    await runner._run_once()

    assert dispatched == ["alice", "bob"]
    assert marked == [("alice", "E1"), ("bob", "E1")]


@pytest.mark.asyncio
async def test_run_once_skips_person_when_already_processed(monkeypatch):
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
    runner = EventListenerRunner(ctx)  # type: ignore[arg-type]

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
    monkeypatch.setattr(runner, "_mark_processed_for_person", lambda person, incoming: None)

    await runner._run_once()

    assert dispatched == ["alice"]


@pytest.mark.asyncio
async def test_build_person_subscriptions_uses_cached_channel_resolution(monkeypatch):
    alice = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        profile={
            "chat": {
                "subscriptions": [
                    {"service": "slack", "channel_name": "dev-chat", "event_source": "socket_mode"}
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
    runner._subscription_channel_cache["alice"] = (tuple(), {"C1"})
    runner._last_group_log_state = (1, 1)

    await runner._aclose_sources()

    assert stopped == ["l1"]
    assert runner._listeners == {}
    assert runner._listener_tokens == {}
    assert runner._subscription_channel_cache == {}
    assert runner._last_group_log_state is None


@pytest.mark.asyncio
async def test_build_person_subscriptions_skips_person_with_missing_app_token(monkeypatch):
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
        "skipped person=%s" in str(args[0]) and len(args) >= 2 and args[1] == "bob"
        for args in ctx.warnings
    )
