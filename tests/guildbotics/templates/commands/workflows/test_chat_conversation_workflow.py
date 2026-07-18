from __future__ import annotations

import json
import types

import pytest

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.entities.team import Person, Role
from guildbotics.integrations.chat_service import (
    ChatEvent,
    ChatEventPage,
    ChatIdentity,
    ChatPostResult,
)
from guildbotics.integrations.chat_state_store import (
    ThreadConversationState,
    ThreadHandoffState,
    ThreadMessageState,
    ThreadSystemNoticeState,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.intelligences.brains.cli_agent import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)
from guildbotics.runtime.event_listener import (
    INCOMING_CHAT_EVENT_KEY,
    IncomingChatEvent,
)
from guildbotics.templates.commands.workflows import chat_conversation_workflow
from guildbotics.utils.i18n_tool import t


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("GUILDBOTICS_DATA_DIR", str(tmp_path / "data"))


class StubLogger:
    def __init__(self) -> None:
        self.lines: list[tuple] = []

    def info(self, *args):
        self.lines.append(("info",) + args)

    def warning(self, *args):
        self.lines.append(("warning",) + args)

    def error(self, *args):
        self.lines.append(("error",) + args)


class FakeChatService:
    def __init__(self) -> None:
        self.identity = ChatIdentity(user_id="U_ALICE", display_name="AliceBot")
        self.posts: list[tuple[str, str, str | None, dict[str, object] | None]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.fail_identity = False
        self.fail_post = False

    async def get_bot_identity(self) -> ChatIdentity:
        if self.fail_identity:
            raise RuntimeError("invalid_auth")
        return self.identity

    async def list_thread_events(
        self, channel_id, *, thread_ts, cursor=None, limit=100
    ) -> ChatEventPage:
        return ChatEventPage(events=[])

    async def post_message(self, channel_id, text, *, thread_ts=None, metadata=None):
        if self.fail_post:
            raise RuntimeError("is_archived")
        self.posts.append((channel_id, text, thread_ts, metadata))
        return ChatPostResult(
            channel_id=channel_id, message_ts="300.1", thread_ts=thread_ts or "300.1"
        )

    def normalize_participant_text(self, text, participant_labels):
        for user_id, label in participant_labels.items():
            text = text.replace(f"<@{user_id}>", f"@{label}")
        return text

    def render_participant_text(self, text, participant_labels):
        return text


class FakeInvokeContext(types.SimpleNamespace):
    def __init__(self, action: str) -> None:
        person = types.SimpleNamespace(
            person_id="alice",
            name="Alice",
            profile={"chat": {"subscriptions": [{"service": "slack"}]}},
        )
        super().__init__(
            person=person,
            logger=StubLogger(),
            language_name="日本語",
            shared_state={},
        )
        self.action = action
        self.invocations: list[tuple[str, dict]] = []
        # When set, only the Nth handle_chat_event call records a completion, so
        # earlier attempts fail the gate and the workflow retries.
        self.complete_on_attempt: int | None = None
        self._handle_calls = 0

    async def invoke(self, name: str, /, **kwargs):
        self.invocations.append((name, kwargs))
        if name != "functions/handle_chat_event":
            return {}
        self._handle_calls += 1
        if self.action == "rate_limit":
            raise CliAgentExecutionError(
                cli_agent="codex",
                result=CliAgentExecutionResult(
                    stdout="",
                    stderr="rate limit",
                    returncode=75,
                    error_category="rate_limited",
                    error_details={
                        "retry_after_at": "2026-07-04T11:44:00+09:00",
                        "retry_after_text": "11:44 AM",
                    },
                ),
            )
        if self.action == "crash":
            # Simulate the AI CLI tool exiting non-zero (the brain raises).
            raise RuntimeError("agent exited non-zero")
        if (
            self.complete_on_attempt is not None
            and self._handle_calls < self.complete_on_attempt
        ):
            # This attempt records nothing, so the completion gate fails.
            return {"status": "working", "message": "still working"}
        run_id = kwargs["workflow_run_id"]
        store = RunStore()
        if self.action == "reply":
            store.append_evidence(
                run_id,
                "chat_reply",
                {
                    "service": "slack",
                    "channel_id": kwargs["channel_id"],
                    "message_ts": "200.1",
                    "thread_ts": kwargs["thread_ts"],
                    "text": "確認します。",
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
        elif self.action == "reaction":
            store.append_evidence(
                run_id,
                "chat_reaction",
                {
                    "service": "slack",
                    "channel_id": kwargs["channel_id"],
                    "message_ts": kwargs["message_ts"],
                    "reaction": "ack",
                    "reacted": True,
                },
            )
            store.complete_run(
                run_id,
                "done",
                "Added a reaction.",
                subject_type="chat",
                subject_id=(
                    f"slack:{kwargs['channel_id']}:"
                    f"{kwargs['thread_ts']}:{kwargs['event_id']}"
                ),
                person_id=kwargs["person_id"],
            )
        elif self.action == "noop":
            store.append_evidence(
                run_id,
                "chat_noop",
                {
                    "service": "slack",
                    "channel_id": kwargs["channel_id"],
                    "thread_ts": kwargs["thread_ts"],
                    "event_id": kwargs["event_id"],
                    "reason": "No response needed.",
                    "noop": True,
                },
            )
            store.complete_run(
                run_id,
                "done",
                "No response needed.",
                subject_type="chat",
                subject_id=(
                    f"slack:{kwargs['channel_id']}:"
                    f"{kwargs['thread_ts']}:{kwargs['event_id']}"
                ),
                person_id=kwargs["person_id"],
            )
        return {"status": "done", "message": "done"}


def _set_incoming_event(
    ctx: types.SimpleNamespace,
    *,
    event_id: str = "E1",
    message_ts: str = "100.1",
    text: str = "<@U_ALICE> please check",
    mentions: list[str] | None = None,
    chat_participation: str = "strict",
) -> None:
    ctx.shared_state[INCOMING_CHAT_EVENT_KEY] = IncomingChatEvent(
        service_name="slack",
        channel_id="C1",
        event=ChatEvent(
            event_id=event_id,
            channel_id="C1",
            message_ts=message_ts,
            thread_ts="100.1",
            author_id="U_USER",
            text=text,
            mentions=list(mentions if mentions is not None else ["U_ALICE"]),
        ),
        chat_participation=chat_participation,
    ).to_shared_state()


@pytest.mark.asyncio
async def test_workflow_delegates_to_handle_chat_event_and_updates_reply_state(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(ctx)

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert service.posts == []
    assert service.reactions == []
    assert ctx.invocations[0][0] == "functions/handle_chat_event"
    kwargs = ctx.invocations[0][1]
    assert kwargs["person_id"] == "alice"
    assert kwargs["service_name"] == "slack"
    assert kwargs["channel_id"] == "C1"
    execution_context = kwargs["agent_execution_context"]
    assert execution_context["run_id"] == kwargs["workflow_run_id"]
    assert execution_context["work_kind"] == "chat"
    assert execution_context["work_identity"] == "slack:U_ALICE:C1:100.1"
    assert execution_context["context_cursor"] == "100.1"
    assert execution_context["event_id"] == "E1"
    assert execution_context["resume_policy"] == "auto"
    # The continuation prompt names the exact run/event and the missing
    # completion record, so a resumed session cannot mistake another run's
    # completion for this one.
    assert execution_context["continuation_input"] == t(
        "commands.workflows.common.agent_chat_continuation",
        run_id=kwargs["workflow_run_id"],
        event_id="E1",
    )
    assert kwargs["workflow_run_id"] in execution_context["continuation_input"]
    assert "E1" in execution_context["continuation_input"]
    assert kwargs["cwd"].name == "alice"
    assert kwargs["handoff_candidates"] == "[]"
    assert kwargs["chat_participation"] == "strict"
    assert "team_profiles" not in kwargs
    # The capability reference is no longer injected per-prompt; the agent reads
    # it from the mandatory `member context` call (the single source of truth).
    assert "chat_capability_help" not in kwargs
    # The shared workflow envelope is injected from the single i18n source.
    assert "guildbotics_execution_mode=workflow" in kwargs["workflow_contract"]
    assert "guildbotics member context --person alice" in kwargs["workflow_contract"]

    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E1"]
    thread_messages = state_store.load_thread_messages("slack", "alice", "C1", "100.1")
    assert [message.message_ts for message in thread_messages] == ["100.1", "200.1"]
    assert thread_messages[1].is_bot_message is True
    thread_state = state_store.load_thread_state("slack", "alice", "C1", "100.1")
    assert "alice" in thread_state.participants


@pytest.mark.asyncio
async def test_redispatch_of_completed_run_skips_agent_and_reuses_evidence(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    # The "crash" action raises if the agent is invoked, so the test fails
    # loudly if the completed run is re-executed.
    ctx = FakeInvokeContext("crash")
    _set_incoming_event(ctx)
    run_id = "run-recovered"
    store = RunStore()
    store.append_evidence(
        run_id,
        "chat_reply",
        {
            "service": "slack",
            "channel_id": "C1",
            "message_ts": "200.1",
            "thread_ts": "100.1",
            "text": "確認します。",
            "posted": True,
        },
    )
    store.complete_run(
        run_id,
        "done",
        "Posted a reply.",
        subject_type="chat",
        subject_id="slack:C1:100.1:E1",
        person_id="alice",
    )
    ctx.shared_state["retry_context"] = {
        "attempt_count": 2,
        "max_attempts": 5,
        "is_final_attempt": False,
        "run_id": run_id,
    }

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    # A crash between the agent's completion record and the dispatcher marking
    # the event processed must resume from the recorded evidence: the agent is
    # never re-invoked (no duplicated replies/reactions) and the event still
    # terminalizes normally.
    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E1"]
    thread_messages = state_store.load_thread_messages("slack", "alice", "C1", "100.1")
    assert [message.message_ts for message in thread_messages] == ["100.1", "200.1"]
    assert thread_messages[1].is_bot_message is True
    thread_state = state_store.load_thread_state("slack", "alice", "C1", "100.1")
    assert "alice" in thread_state.participants


@pytest.mark.asyncio
async def test_two_messages_in_one_thread_share_conversation_and_advance_cursor(
    tmp_path,
) -> None:
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    first = FakeInvokeContext("reply")
    _set_incoming_event(first, event_id="E1", message_ts="100.1")
    await chat_conversation_workflow.main(
        first, chat_service=service, state_store=state_store
    )
    second = FakeInvokeContext("reply")
    _set_incoming_event(second, event_id="E2", message_ts="101.1")
    await chat_conversation_workflow.main(
        second, chat_service=service, state_store=state_store
    )

    first_context = first.invocations[0][1]["agent_execution_context"]
    second_context = second.invocations[0][1]["agent_execution_context"]
    assert first_context["work_identity"] == second_context["work_identity"]
    assert first_context["context_cursor"] == "100.1"
    assert second_context["context_cursor"] == "101.1"
    assert second_context["rebuild_context_complete"] is True
    assert second_context["attempt"] == 1
    rebuilt = json.loads(second_context["rebuild_context"])
    assert len(rebuilt) == 3
    contents = [message["content"] for message in rebuilt]
    assert contents.count("@alice please check") == 2
    assert "確認します。" in contents
    assert {message["timestamp"] for message in rebuilt} == {
        "100.1",
        "101.1",
        "200.1",
    }


@pytest.mark.asyncio
async def test_live_thread_snapshot_paginates_and_keeps_latest_bound() -> None:
    class PaginatedChatService(FakeChatService):
        def __init__(self) -> None:
            super().__init__()
            self.cursors: list[str | None] = []

        async def list_thread_events(
            self, channel_id, *, thread_ts, cursor=None, limit=100
        ) -> ChatEventPage:
            self.cursors.append(cursor)
            start, stop, next_cursor = (
                (1, 101, "page-2") if cursor is None else (101, 151, None)
            )
            return ChatEventPage(
                events=[
                    ChatEvent(
                        event_id=f"E{index}",
                        channel_id=channel_id,
                        message_ts=f"{index}.1",
                        thread_ts=thread_ts,
                        author_id="U_USER",
                        text=f"message-{index}",
                    )
                    for index in range(start, stop)
                ],
                cursor=next_cursor,
            )

    service = PaginatedChatService()
    payload = await chat_conversation_workflow._build_agent_prompt_payload(
        context=FakeInvokeContext("noop"),
        chat_service=service,
        event=ChatEvent(
            event_id="E150",
            channel_id="C1",
            message_ts="150.1",
            thread_ts="1.1",
            author_id="U_USER",
            text="message-150",
        ),
        thread_messages=[],
        self_user_id="U_ALICE",
        thread_state=ThreadConversationState(channel_id="C1", thread_ts="1.1"),
    )

    assert service.cursors == [None, "page-2"]
    assert payload["thread_context_complete"] is True
    assert len(payload["thread_context"]) == 100
    assert payload["thread_context"][0]["timestamp"] == "51.1"
    assert payload["thread_context"][-1]["timestamp"] == "150.1"


@pytest.mark.asyncio
async def test_reaction_only_completion_processes_without_bot_message(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reaction")
    _set_incoming_event(ctx)

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E1"]
    thread_messages = state_store.load_thread_messages("slack", "alice", "C1", "100.1")
    assert [message.message_ts for message in thread_messages] == ["100.1"]
    # A reaction is a visible action, so the member is recorded as a participant.
    thread_state = state_store.load_thread_state("slack", "alice", "C1", "100.1")
    assert "alice" in thread_state.participants


@pytest.mark.asyncio
async def test_noop_completion_processes_without_visible_action(tmp_path, monkeypatch):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(ctx)

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E1"]
    # noop takes no visible action, so the member must not be recorded as a
    # thread participant.
    thread_state = state_store.load_thread_state("slack", "alice", "C1", "100.1")
    assert "alice" not in thread_state.participants


@pytest.mark.asyncio
async def test_unmentioned_new_thread_is_processed_without_agent(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(ctx, text="please check", mentions=[])

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E1"]
    thread_messages = state_store.load_thread_messages("slack", "alice", "C1", "100.1")
    assert thread_messages == []


@pytest.mark.asyncio
async def test_social_unmentioned_new_thread_delegates(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(
        ctx,
        text="今日のランチどうします?",
        mentions=[],
        chat_participation="social",
    )

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert ctx.invocations[0][0] == "functions/handle_chat_event"
    kwargs = ctx.invocations[0][1]
    assert kwargs["chat_participation"] == "social"
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E1"]


@pytest.mark.asyncio
async def test_unmentioned_followup_after_prior_mention_delegates(tmp_path):
    service = FakeChatService()
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
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(
        ctx, event_id="E2", message_ts="100.2", text="Any update?", mentions=[]
    )

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert ctx.invocations[0][0] == "functions/handle_chat_event"
    kwargs = ctx.invocations[0][1]
    assert kwargs["event_id"] == "E2"
    assert kwargs["message_ts"] == "100.2"


@pytest.mark.asyncio
async def test_muted_unmentioned_followup_after_prior_mention_skips(tmp_path):
    service = FakeChatService()
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
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(
        ctx,
        event_id="E2",
        message_ts="100.2",
        text="Any update?",
        mentions=[],
        chat_participation="muted",
    )

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E2"]


@pytest.mark.asyncio
async def test_followup_mentioning_other_member_skips_even_after_prior_mention(
    tmp_path,
):
    service = FakeChatService()
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
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(
        ctx,
        event_id="E2",
        message_ts="100.2",
        text="<@U_BOB> can you check?",
        mentions=["U_BOB"],
    )

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E2"]


def test_author_labels_include_mentionable_team_members_not_in_thread():
    ctx = types.SimpleNamespace(person=types.SimpleNamespace(person_id="alice"))
    labels = chat_conversation_workflow._build_author_labels(
        ctx,
        "U_ALICE",
        ChatEvent(
            event_id="E1",
            channel_id="C1",
            message_ts="100.1",
            thread_ts="100.1",
            author_id="U_USER",
            text="please check",
        ),
        [],
        {"U_ALICE": "alice", "U_BOB": "bob"},
    )

    assert labels["U_ALICE"] == "alice"
    assert labels["U_BOB"] == "bob"


def test_handoff_candidates_include_only_mentionable_other_members_with_roles():
    alice = Person(
        person_id="alice",
        name="Alice",
        roles={"product": Role(id="product", summary="Product", description="")},
    )
    bob = Person(
        person_id="bob",
        name="Bob",
        is_active=False,
        roles={"design": Role(id="design", summary="Design", description="UX")},
        speaking_style="verbose",
        profile={"character": {"archetype": "designer"}},
    )
    carol = Person(
        person_id="carol",
        name="Carol",
        roles={
            "operations": Role(id="operations", summary="Operations", description="Ops")
        },
    )
    ctx = types.SimpleNamespace(
        person=alice, team=types.SimpleNamespace(members=[alice, bob, carol])
    )

    candidates = chat_conversation_workflow._build_handoff_candidates(
        ctx, {"U_ALICE": "alice", "U_BOB": "bob"}
    )

    assert candidates == [
        {
            "person_id": "bob",
            "name": "Bob",
            "mention": "@bob",
            "roles": {"design": {"summary": "Design", "description": "UX"}},
        }
    ]


@pytest.mark.asyncio
async def test_chat_user_to_person_labels_uses_configured_slack_user_id():
    alice = Person(person_id="alice", name="Alice")
    bob = Person(
        person_id="bob",
        name="Bob",
        account_info={"slack_user_id": "U_BOB"},
    )
    ctx = types.SimpleNamespace(
        team=types.SimpleNamespace(members=[alice, bob]),
        clone_for=lambda member: (_ for _ in ()).throw(
            AssertionError(member.person_id)
        ),
    )

    labels = await chat_conversation_workflow._chat_user_to_person_labels(ctx)

    assert labels == {"U_BOB": "bob"}


def test_record_handoffs_saves_mentions_to_known_members():
    alice = Person(person_id="alice", name="Alice")
    bob = Person(
        person_id="bob",
        name="Bob",
        roles={"design": Role(id="design", summary="Design", description="")},
    )
    ctx = types.SimpleNamespace(team=types.SimpleNamespace(members=[alice, bob]))
    thread_state = ThreadConversationState(channel_id="C1", thread_ts="100.1")

    chat_conversation_workflow._record_handoffs(
        context=ctx,
        thread_state=thread_state,
        participant_labels={"U_ALICE": "alice", "U_BOB": "bob"},
        mentioned_user_ids=["U_BOB"],
        source_person_id="alice",
        message_ts="200.1",
        text="<@U_BOB> design観点を見てもらえますか?",
    )

    assert len(thread_state.handoffs) == 1
    handoff = thread_state.handoffs[0]
    assert handoff.person_id == "bob"
    assert handoff.roles == ["design"]
    assert handoff.message_ts == "200.1"
    assert handoff.text == "<@U_BOB> design観点を見てもらえますか?"


def test_record_handoffs_ignores_non_team_participant_labels():
    alice = Person(person_id="alice", name="Alice")
    ctx = types.SimpleNamespace(team=types.SimpleNamespace(members=[alice]))
    thread_state = ThreadConversationState(channel_id="C1", thread_ts="100.1")

    chat_conversation_workflow._record_handoffs(
        context=ctx,
        thread_state=thread_state,
        participant_labels={"U_ALICE": "alice", "U_USER": "user_1"},
        mentioned_user_ids=["U_USER"],
        source_person_id="alice",
        message_ts="200.1",
        text="<@U_USER> どう思いますか?",
    )

    assert thread_state.handoffs == []


@pytest.mark.asyncio
async def test_prompt_payload_includes_existing_handoffs():
    thread_state = ThreadConversationState(
        channel_id="C1",
        thread_ts="100.1",
        handoffs=[
            ThreadHandoffState(
                person_id="bob",
                roles=["design"],
                message_ts="200.1",
                text="@bob design観点を見てもらえますか?",
            )
        ],
    )
    ctx = FakeInvokeContext("noop")

    payload = await chat_conversation_workflow._build_agent_prompt_payload(
        context=ctx,
        chat_service=FakeChatService(),
        event=ChatEvent(
            event_id="E2",
            channel_id="C1",
            message_ts="201.1",
            thread_ts="100.1",
            author_id="U_USER",
            text="Any thoughts?",
        ),
        thread_messages=[],
        self_user_id="U_ALICE",
        thread_state=thread_state,
    )

    assert payload["previous_thread_context"]["handoffs"] == [
        {
            "person_id": "bob",
            "roles": ["design"],
            "message_ts": "200.1",
            "text": "@bob design観点を見てもらえますか?",
            "thread_topic": "",
            "latest_focus": "",
        }
    ]


@pytest.mark.asyncio
async def test_incomplete_turns_retry_then_escalate(tmp_path, monkeypatch):
    from guildbotics.utils.i18n_tool import t

    monkeypatch.setenv("GUILDBOTICS_CHAT_MAX_ATTEMPTS", "3")
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("missing")
    _set_incoming_event(ctx)
    ctx.shared_state["retry_context"] = {
        "attempt_count": 3,
        "max_attempts": 3,
        "is_final_attempt": True,
        "run_id": "run-1",
    }

    # A single dispatch retries the agent in-process up to the budget, then
    # escalates and stops (no exception bubbles out).
    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    handle_calls = [
        kwargs
        for name, kwargs in ctx.invocations
        if name == "functions/handle_chat_event"
    ]
    assert len(handle_calls) == 2
    # All attempts share one run id and the same provider-neutral conversation.
    run_ids = {kwargs["workflow_run_id"] for kwargs in handle_calls}
    assert len(run_ids) == 1
    conversation_keys = {
        kwargs["agent_execution_context"]["work_identity"] for kwargs in handle_calls
    }
    assert conversation_keys == {"slack:U_ALICE:C1:100.1"}
    assert [call["agent_execution_context"]["attempt"] for call in handle_calls] == [
        3,
        4,
    ]

    # Escalated to the thread and stopped (event marked processed, no re-dispatch).
    assert state_store.load_channel_cursor(
        "slack", "alice", "C1"
    ).processed_event_ids == ["E1"]
    assert len(service.posts) == 1
    channel_id, text, thread_ts, metadata = service.posts[0]
    assert channel_id == "C1"
    assert thread_ts == "100.1"
    assert text == t(
        "commands.workflows.chat_conversation_workflow.incomplete_escalation"
    )
    assert metadata is not None
    assert metadata["event_type"] == "guildbotics.workflow_status"
    assert metadata["event_payload"]["routing"] == "suppress"
    assert metadata["event_payload"]["reason"] == "failed"
    # Giving up must be visible in the logs as an error, not silent.
    error_lines = [line for line in ctx.logger.lines if line[0] == "error"]
    assert len(error_lines) == 1
    assert error_lines[0][1].startswith("chat event abandoned after final attempt")


@pytest.mark.asyncio
async def test_agent_run_failure_escalates_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CHAT_MAX_ATTEMPTS", "2")
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("crash")  # the agent run raises every attempt
    _set_incoming_event(ctx)
    ctx.shared_state["retry_context"] = {
        "attempt_count": 2,
        "max_attempts": 2,
        "is_final_attempt": True,
        "run_id": "run-1",
    }

    # A failing agent run must be bounded and escalated, NOT raise out of the
    # workflow (which would leave the event queued for infinite retry).
    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    handle_calls = [
        kwargs
        for name, kwargs in ctx.invocations
        if name == "functions/handle_chat_event"
    ]
    assert len(handle_calls) == 1  # invoke exceptions are left to pending backoff
    assert len(service.posts) == 1  # escalated to the thread
    assert state_store.load_channel_cursor(
        "slack", "alice", "C1"
    ).processed_event_ids == ["E1"]


@pytest.mark.asyncio
async def test_non_final_agent_run_failure_bubbles_for_pending_backoff(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("crash")
    _set_incoming_event(ctx)
    ctx.shared_state["retry_context"] = {
        "attempt_count": 1,
        "max_attempts": 5,
        "is_final_attempt": False,
        "run_id": "run-1",
    }

    with pytest.raises(RuntimeError, match="agent exited non-zero"):
        await chat_conversation_workflow.main(
            ctx, chat_service=service, state_store=state_store
        )

    assert service.posts == []
    assert (
        state_store.load_channel_cursor("slack", "alice", "C1").processed_event_ids
        == []
    )


@pytest.mark.asyncio
async def test_missing_retry_context_agent_failure_bubbles_for_pending_backoff(
    tmp_path,
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("crash")
    _set_incoming_event(ctx)

    with pytest.raises(RuntimeError, match="agent exited non-zero"):
        await chat_conversation_workflow.main(
            ctx, chat_service=service, state_store=state_store
        )

    assert service.posts == []
    assert (
        state_store.load_channel_cursor("slack", "alice", "C1").processed_event_ids
        == []
    )


@pytest.mark.asyncio
async def test_rate_limit_posts_notice_and_leaves_event_pending(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("rate_limit")
    _set_incoming_event(ctx)

    with pytest.raises(CliAgentExecutionError):
        await chat_conversation_workflow.main(
            ctx, chat_service=service, state_store=state_store
        )

    assert len(service.posts) == 1
    _channel_id, text, _thread_ts, metadata = service.posts[0]
    assert "11:44 AM" in text
    assert "2026-07-04T11:44:00+09:00" not in text
    assert metadata is not None
    payload = metadata["event_payload"]
    assert payload["reason"] == "rate_limited"
    assert payload["routing"] == "suppress"
    assert payload["retry_after_at"] == "2026-07-04T11:44:00+09:00"
    assert (
        state_store.load_channel_cursor("slack", "alice", "C1").processed_event_ids
        == []
    )
    thread_state = state_store.load_thread_state("slack", "alice", "C1", "100.1")
    assert len(thread_state.system_notices) == 1
    assert thread_state.system_notices[0].reason == "rate_limited"


@pytest.mark.asyncio
async def test_duplicate_system_notice_is_not_posted_again(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    state = ThreadConversationState(channel_id="C1", thread_ts="100.1")
    state.system_notices.append(
        ThreadSystemNoticeState(
            kind="workflow_error",
            reason="failed",
            person_id="alice",
            source_event_id="E1",
            message_ts="300.1",
        )
    )
    state_store.save_thread_state("slack", "alice", "C1", "100.1", state)
    ctx = FakeInvokeContext("crash")
    _set_incoming_event(ctx)
    ctx.shared_state["retry_context"] = {
        "attempt_count": 5,
        "max_attempts": 5,
        "is_final_attempt": True,
        "run_id": "run-1",
    }

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert service.posts == []
    assert state_store.load_channel_cursor(
        "slack", "alice", "C1"
    ).processed_event_ids == ["E1"]


@pytest.mark.asyncio
async def test_final_notice_post_failure_marks_processed_without_notice_state(tmp_path):
    service = FakeChatService()
    service.fail_post = True
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("crash")
    _set_incoming_event(ctx)
    ctx.shared_state["retry_context"] = {
        "attempt_count": 5,
        "max_attempts": 5,
        "is_final_attempt": True,
        "run_id": "run-1",
    }

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert service.posts == []
    assert state_store.load_channel_cursor(
        "slack", "alice", "C1"
    ).processed_event_ids == ["E1"]
    thread_state = state_store.load_thread_state("slack", "alice", "C1", "100.1")
    assert thread_state.system_notices == []


@pytest.mark.asyncio
async def test_final_prerun_identity_failure_marks_processed(tmp_path):
    service = FakeChatService()
    service.fail_identity = True
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(ctx)
    ctx.shared_state["retry_context"] = {
        "attempt_count": 5,
        "max_attempts": 5,
        "is_final_attempt": True,
        "run_id": "run-1",
    }

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert ctx.invocations == []
    assert state_store.load_channel_cursor(
        "slack", "alice", "C1"
    ).processed_event_ids == ["E1"]
    # Giving up must be visible in the logs as an error, not silent.
    error_lines = [line for line in ctx.logger.lines if line[0] == "error"]
    assert len(error_lines) == 1
    assert error_lines[0][1].startswith("chat event abandoned after final attempt")


@pytest.mark.asyncio
async def test_non_final_prerun_identity_failure_bubbles(tmp_path):
    service = FakeChatService()
    service.fail_identity = True
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(ctx)
    ctx.shared_state["retry_context"] = {
        "attempt_count": 1,
        "max_attempts": 5,
        "is_final_attempt": False,
        "run_id": "run-1",
    }

    with pytest.raises(RuntimeError, match="invalid_auth"):
        await chat_conversation_workflow.main(
            ctx, chat_service=service, state_store=state_store
        )

    assert (
        state_store.load_channel_cursor("slack", "alice", "C1").processed_event_ids
        == []
    )


@pytest.mark.asyncio
async def test_completion_on_retry_stops_early(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CHAT_MAX_ATTEMPTS", "5")
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    # Completes only on the second attempt (the continuation turn).
    ctx = FakeInvokeContext("reply")
    ctx.complete_on_attempt = 2
    _set_incoming_event(ctx)

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    handle_calls = [
        kwargs
        for name, kwargs in ctx.invocations
        if name == "functions/handle_chat_event"
    ]
    # Stops as soon as a turn records a terminal completion: no extra retries.
    assert len(handle_calls) == 2
    # Both attempts reuse the same run id and conversation key.
    assert handle_calls[0]["workflow_run_id"] == handle_calls[1]["workflow_run_id"]
    assert (
        handle_calls[0]["agent_execution_context"]["work_identity"]
        == handle_calls[1]["agent_execution_context"]["work_identity"]
    )
    assert [call["agent_execution_context"]["attempt"] for call in handle_calls] == [
        1,
        2,
    ]
    assert service.posts == []  # no escalation; it completed
    assert state_store.load_channel_cursor(
        "slack", "alice", "C1"
    ).processed_event_ids == ["E1"]


@pytest.mark.asyncio
async def test_obvious_self_message_is_marked_processed_without_agent(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    ctx.shared_state[INCOMING_CHAT_EVENT_KEY] = IncomingChatEvent(
        service_name="slack",
        channel_id="C1",
        event=ChatEvent(
            event_id="E_SELF",
            channel_id="C1",
            message_ts="100.1",
            thread_ts="100.1",
            author_id="U_ALICE",
            text="bot message",
        ),
    ).to_shared_state()

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E_SELF"]


@pytest.mark.asyncio
async def test_chat_conversation_workflow_reads_from_invocation(tmp_path):
    from guildbotics.runtime.workflow_invocation import (
        WORKFLOW_INVOCATION_KEY,
        WorkflowInvocation,
    )

    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")

    incoming = IncomingChatEvent(
        service_name="slack",
        channel_id="C1",
        event=ChatEvent(
            event_id="E_INVOCATION",
            channel_id="C1",
            message_ts="100.1",
            thread_ts="100.1",
            author_id="U_BOB",
            text="hello bot",
            mentions=["U_ALICE"],
        ),
    )

    inv = WorkflowInvocation(
        command="workflows/chat_conversation_workflow",
        person_id="alice",
        source="event_queue",
        trigger_type="chat",
        payload=incoming.to_shared_state(),
    )
    ctx.shared_state[WORKFLOW_INVOCATION_KEY] = inv

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert len(ctx.invocations) == 1
    assert ctx.invocations[0][0] == "functions/handle_chat_event"

    import json

    latest_msg = json.loads(ctx.invocations[0][1].get("latest_message", "{}"))
    assert latest_msg.get("content") == "hello bot"

    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert "E_INVOCATION" in channel_state.processed_event_ids


@pytest.mark.asyncio
async def test_final_attempt_abandon_records_dispatch_abandoned_event(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GUILDBOTICS_CHAT_MAX_ATTEMPTS", "3")
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("missing")
    _set_incoming_event(ctx)
    ctx.shared_state["retry_context"] = {
        "attempt_count": 3,
        "max_attempts": 3,
        "is_final_attempt": True,
        "run_id": "run-1",
    }
    recorded: list[dict] = []
    monkeypatch.setattr(
        chat_conversation_workflow,
        "record_chat_dispatch_abandoned",
        lambda **kwargs: recorded.append(kwargs),
    )

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert len(recorded) == 1
    assert recorded[0]["event_id"] == "E1"
    assert recorded[0]["run_id"] == "run-1"
    assert recorded[0]["attempt_count"] == 3
    assert recorded[0]["max_attempts"] == 3


@pytest.mark.asyncio
async def test_recovered_completion_records_workflow_completed_event(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("crash")
    _set_incoming_event(ctx)
    run_id = "run-recovered-event"
    store = RunStore()
    store.append_evidence(
        run_id,
        "chat_reply",
        {
            "service": "slack",
            "channel_id": "C1",
            "message_ts": "200.1",
            "thread_ts": "100.1",
            "text": "確認します。",
            "posted": True,
        },
    )
    store.complete_run(
        run_id,
        "done",
        "Posted a reply.",
        subject_type="chat",
        subject_id="slack:C1:100.1:E1",
        person_id="alice",
    )
    ctx.shared_state["retry_context"] = {
        "attempt_count": 2,
        "max_attempts": 5,
        "is_final_attempt": False,
        "run_id": run_id,
    }
    recorded: list[dict] = []
    monkeypatch.setattr(
        chat_conversation_workflow,
        "record_workflow_completed",
        lambda **kwargs: recorded.append(kwargs),
    )

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert recorded == [{"run_id": run_id, "recovered": True}]
