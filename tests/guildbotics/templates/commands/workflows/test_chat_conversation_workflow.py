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
from guildbotics.intelligences.decisions.models import Selection
from guildbotics.runtime.event_listener import IncomingChatEvent
from guildbotics.runtime.workflow_invocation import (
    WORKFLOW_INVOCATION_KEY,
    WorkflowInvocation,
)
from guildbotics.templates.commands.workflows import chat_conversation_workflow
from guildbotics.utils.i18n_tool import t


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))


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

    async def add_reaction(self, channel_id, message_ts, reaction):
        self.reactions.append((channel_id, message_ts, reaction))


@pytest.fixture(autouse=True)
def _judgment_engine(monkeypatch):
    async def assess(state, config, *, logger, **kwargs):
        ctx = logger.context
        ctx.assessments.append(state)
        return Selection(
            route="agent",
            reason=ctx.decision_reason,
            response_effort=ctx.response_effort,
        ), "a" * 32

    monkeypatch.setattr(chat_conversation_workflow, "assess", assess)


def _agent_invocations(ctx) -> list[tuple[str, dict]]:
    """Only the agent turns, skipping the per-event judgment."""
    return [
        item for item in ctx.invocations if item[0] == "functions/handle_chat_event"
    ]


class FakeInvokeContext(types.SimpleNamespace):
    brain_factory = None

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
        self.logger.context = self
        self.assessments = []
        self.invocations: list[tuple[str, dict]] = []
        # When set, only the Nth handle_chat_event call records a completion, so
        # earlier attempts fail the gate and the workflow retries.
        self.complete_on_attempt: int | None = None
        self._handle_calls = 0
        self.decision_reason = "request"
        self.response_effort = None
        # Stand-ins for Context.pipe and what each invoked command received as
        # its user message.
        self.pipe = ""
        self.consumed_messages: list[tuple[str, str]] = []

    async def invoke(self, name: str, /, **kwargs):
        self.invocations.append((name, kwargs))
        # Mirror CommandRunner: a command without an explicit `message` reads
        # `Context.pipe`, and every command's output replaces it. Recording what
        # each call would actually receive keeps a leaked pipe visible here.
        self.consumed_messages.append((name, kwargs.get("message", self.pipe)))
        result = await self._invoke(name, **kwargs)
        self.pipe = "" if result is None else str(result)
        return result

    async def _invoke(self, name: str, /, **kwargs):
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
    incoming = IncomingChatEvent(
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
    )
    ctx.shared_state[WORKFLOW_INVOCATION_KEY] = WorkflowInvocation(
        command="workflows/chat_conversation_workflow",
        person_id="alice",
        source="event_queue",
        trigger_type="chat",
        payload=incoming.to_shared_state(),
    )


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
    kwargs = _agent_invocations(ctx)[0][1]
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
    ctx.shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"] = {
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
    first.response_effort = "high"
    _set_incoming_event(first, event_id="E1", message_ts="100.1")
    await chat_conversation_workflow.main(
        first, chat_service=service, state_store=state_store
    )
    second = FakeInvokeContext("reply")
    second.response_effort = "default"
    _set_incoming_event(second, event_id="E2", message_ts="101.1")
    await chat_conversation_workflow.main(
        second, chat_service=service, state_store=state_store
    )

    first_context = _agent_invocations(first)[0][1]["agent_execution_context"]
    second_context = _agent_invocations(second)[0][1]["agent_execution_context"]
    assert _agent_invocations(first)[0][1]["effort"] == "high"
    assert _agent_invocations(second)[0][1]["effort"] == "high"
    assert first_context["work_identity"] == second_context["work_identity"]
    assert first_context["context_cursor"] == "100.1"
    assert second_context["context_cursor"] == "101.1"
    assert second_context["rebuild_context_complete"] is True
    assert second_context["attempt"] == 1
    rebuilt = json.loads(second_context["rebuild_context"])
    assert len(rebuilt) == 1
    contents = [message["content"] for message in rebuilt]
    assert contents.count("@alice please check") == 1
    assert "確認します。" not in contents
    assert {message["timestamp"] for message in rebuilt} == {
        "100.1",
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
    assert payload["thread_context"][0]["timestamp"] == "50.1"
    assert payload["thread_context"][-1]["timestamp"] == "149.1"


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

    kwargs = _agent_invocations(ctx)[0][1]
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

    kwargs = _agent_invocations(ctx)[0][1]
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


class MentionSnapshotChatService(FakeChatService):
    """Serves a thread snapshot whose first message mentions the member."""

    async def list_thread_events(
        self, channel_id, *, thread_ts, cursor=None, limit=100
    ) -> ChatEventPage:
        return ChatEventPage(
            events=[
                ChatEvent(
                    event_id="E1",
                    channel_id=channel_id,
                    message_ts="100.1",
                    thread_ts=thread_ts,
                    author_id="U_USER",
                    text="<@U_ALICE> please check",
                    mentions=["U_ALICE"],
                )
            ]
        )


class UnavailableChatService(FakeChatService):
    async def list_thread_events(
        self, channel_id, *, thread_ts, cursor=None, limit=100
    ) -> ChatEventPage:
        raise RuntimeError("provider unavailable")


@pytest.mark.asyncio
async def test_empty_cache_followup_participates_via_provider_snapshot(tmp_path):
    # Handoff to a device with an empty local chat cache: the provider snapshot
    # alone must yield the same participation decision.
    service = MentionSnapshotChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(
        ctx, event_id="E2", message_ts="100.2", text="Any update?", mentions=[]
    )

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    kwargs = _agent_invocations(ctx)[0][1]
    assert kwargs["event_id"] == "E2"


@pytest.mark.asyncio
async def test_snapshot_is_fetched_once_and_persisted_to_cache(tmp_path):
    # The same provider snapshot must serve the participation decision and the
    # agent prompt (no second fetch that could fail), and it is persisted to
    # the local cache so retries keep the decided-upon context.
    class CountingChatService(MentionSnapshotChatService):
        def __init__(self) -> None:
            super().__init__()
            self.fetch_count = 0

        async def list_thread_events(self, *args, **kwargs):
            self.fetch_count += 1
            return await super().list_thread_events(*args, **kwargs)

    service = CountingChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(
        ctx, event_id="E2", message_ts="100.2", text="Any update?", mentions=[]
    )

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert service.fetch_count == 1
    cached = state_store.load_thread_messages("slack", "alice", "C1", "100.1")
    assert [message.message_ts for message in cached] == ["100.1", "100.2"]
    assert cached[0].mentions == ["U_ALICE"]


@pytest.mark.asyncio
async def test_provider_unavailable_without_cache_keeps_event_unprocessed(tmp_path):
    # Neither the provider nor the local cache can decide participation: the
    # event must bubble as ThreadContextUnavailableError, staying unprocessed
    # and creating no task run.
    service = UnavailableChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(
        ctx, event_id="E2", message_ts="100.2", text="Any update?", mentions=[]
    )

    with pytest.raises(chat_conversation_workflow.ThreadContextUnavailableError):
        await chat_conversation_workflow.main(
            ctx, chat_service=service, state_store=state_store
        )

    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("participation", ["strict", "social", "muted"])
async def test_provider_unavailable_with_cached_mention_waits_for_fresh_input(
    tmp_path,
    participation,
):
    # Cached participation cannot establish whether a newer correction exists.
    service = UnavailableChatService()
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
        mentions=["U_ALICE"],
        chat_participation=participation,
    )

    with pytest.raises(chat_conversation_workflow.ThreadContextUnavailableError):
        await chat_conversation_workflow.main(
            ctx, chat_service=service, state_store=state_store
        )
    assert ctx.invocations == []
    assert not state_store.is_processed_event("slack", "alice", "C1", "E2")


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
    ctx.shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"] = {
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
    ctx.shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"] = {
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
    ctx.shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"] = {
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
    ctx.shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"] = {
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
    ctx.shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"] = {
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
    ctx.shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"] = {
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
    ctx.shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"] = {
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
    ctx.shared_state[WORKFLOW_INVOCATION_KEY] = WorkflowInvocation(
        command="workflows/chat_conversation_workflow",
        person_id="alice",
        source="event_queue",
        trigger_type="chat",
        payload=IncomingChatEvent(
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
        ).to_shared_state(),
    )

    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )

    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("slack", "alice", "C1")
    assert channel_state.processed_event_ids == ["E_SELF"]


@pytest.mark.asyncio
async def test_chat_conversation_workflow_reads_from_invocation(tmp_path):
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

    assert len(_agent_invocations(ctx)) == 1

    import json

    messages = json.loads(_agent_invocations(ctx)[0][1]["unprocessed_messages"])
    assert [message["content"] for message in messages] == ["hello bot"]

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
    ctx.shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"] = {
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
    ctx.shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"] = {
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

    assert recorded == [{"run_id": run_id, "attempt": 2, "recovered": True}]


# --------------------------------------------------------------------------- #
# Judgment: delegation and retries
# --------------------------------------------------------------------------- #


async def _run_chat_event(tmp_path, monkeypatch, ctx, state_store) -> FakeChatService:
    service = FakeChatService()
    _set_incoming_event(ctx)
    await chat_conversation_workflow.main(
        ctx, chat_service=service, state_store=state_store
    )
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["invalid", "context", "request", "work"])
async def test_judgment_without_effort_preserves_response_settings(
    tmp_path, monkeypatch, reason
):
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    ctx.decision_reason = reason
    await _run_chat_event(tmp_path, monkeypatch, ctx, state_store)
    invocation = _agent_invocations(ctx)[0][1]
    assert "effort" not in invocation
    assert "model" not in invocation
    assert "brain" not in invocation
    assert "previous_effort" not in ctx.assessments[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored,candidate,expected",
    [
        ("", "default", "default"),
        ("", "high", "high"),
        ("default", "high", "high"),
        ("high", "default", "high"),
        ("high", None, "high"),
        ("default", None, "default"),
        ("", None, ""),
    ],
)
async def test_response_effort_is_promoted_and_persisted(
    tmp_path, monkeypatch, stored, candidate, expected
):
    state_store = FileConversationStateStore(base_dir=tmp_path)
    state = ThreadConversationState(channel_id="C1", thread_ts="100.1", effort=stored)
    state_store.save_thread_state("slack", "alice", "C1", "100.1", state)
    ctx = FakeInvokeContext("reply")
    ctx.response_effort = candidate
    await _run_chat_event(tmp_path, monkeypatch, ctx, state_store)
    invocation = _agent_invocations(ctx)[0][1]
    assert invocation.get("effort", "") == expected
    assert "model" not in invocation and "brain" not in invocation
    assert (
        state_store.load_thread_state("slack", "alice", "C1", "100.1").effort
        == expected
    )
    evidence = RunStore().evidence(ctx.assessments[0]["run_id"])
    decision = next(
        item["payload"] for item in evidence if item["evidence_type"] == "chat_decision"
    )
    assert decision["response_effort"] == candidate


@pytest.mark.asyncio
async def test_no_invoked_command_inherits_another_command_output_as_input(
    tmp_path, monkeypatch
):
    """Every invocation states its own message, so nothing leaks via the pipe.

    `CommandRunner` writes each command's output to `Context.pipe` and feeds that
    pipe to the next command that does not pass `message`. Without an explicit
    empty message the agent would receive the previous command’s output as if the
    user had typed it.
    """
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")

    await _run_chat_event(tmp_path, monkeypatch, ctx, state_store)

    assert ctx.consumed_messages, "no command was invoked"
    for name, message in ctx.consumed_messages:
        assert message == "", f"{name} received leaked pipe content: {message!r}"


@pytest.mark.asyncio
async def test_judgment_runs_once_per_event_not_per_retry(tmp_path, monkeypatch):
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    ctx.response_effort = "high"
    # The first agent attempt records no completion, so the workflow retries.
    ctx.complete_on_attempt = 2

    await _run_chat_event(tmp_path, monkeypatch, ctx, state_store)

    assessments = ctx.assessments
    assert len(assessments) == 1
    assert len(_agent_invocations(ctx)) == 2
    assert all(call[1]["effort"] == "high" for call in _agent_invocations(ctx))


class SnapshotChatService(FakeChatService):
    def __init__(self, events):
        super().__init__()
        self.events = events

    async def list_thread_events(
        self, channel_id, *, thread_ts, cursor=None, limit=100
    ):
        start = int(cursor or 0)
        end = start + limit
        return ChatEventPage(
            events=self.events[start:end],
            cursor=str(end) if end < len(self.events) else None,
        )


def _batch_event(number, *, text=None, mentions=(), thread_ts="100.1"):
    return ChatEvent(
        event_id=f"E{number}",
        channel_id="C1",
        message_ts=f"{100 + number}.1",
        thread_ts=thread_ts,
        author_id="U_USER",
        text=text or f"message-{number}",
        mentions=list(mentions),
        is_thread_reply=True,
    )


def _batch_context(action="noop", *, run_id="batch-run", attempt=1):
    ctx = FakeInvokeContext(action)
    _set_incoming_event(ctx, event_id="E3", message_ts="103.1")
    ctx.shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"] = {
        "run_id": run_id,
        "attempt_count": attempt,
        "max_attempts": 5,
        "is_final_attempt": False,
    }
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("participation", ["strict", "muted"])
async def test_batch_reads_requests_and_corrections_but_leaves_inflight_arrivals(
    tmp_path,
    participation,
):
    store = FileConversationStateStore(base_dir=tmp_path / "state")
    events = [
        _batch_event(2),
        _batch_event(3, text="Deploy version A", mentions=["U_ALICE"]),
        _batch_event(4, text="Version B fixes the bug", mentions=["U_BOB"]),
        _batch_event(5, text="Correction: deploy version B instead"),
    ]
    for event in (events[1], events[-1]):
        store.upsert_pending_event("slack", "alice", "C1", event, participation)
    store.upsert_pending_event("slack", "bob", "C1", events[-1])
    store.upsert_pending_event(
        "slack", "alice", "C1", _batch_event(8, thread_ts="other")
    )
    service = SnapshotChatService(events)
    ctx = _batch_context()
    ctx.shared_state[WORKFLOW_INVOCATION_KEY].payload["chat_participation"] = (
        participation
    )
    original_invoke = ctx.invoke

    async def invoke(name, **kwargs):
        if name == "functions/handle_chat_event":
            new_event = _batch_event(6, text="Also update the release notes")
            service.events.append(new_event)
            store.upsert_pending_event("slack", "alice", "C1", new_event)
        return await original_invoke(name, **kwargs)

    ctx.invoke = invoke
    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=store)

    assert len(_agent_invocations(ctx)) == 1
    kwargs = _agent_invocations(ctx)[0][1]
    unread = json.loads(kwargs["unprocessed_messages"])
    assert [item["content"] for item in unread] == [item.text for item in events[1:4]]
    assert kwargs["agent_execution_context"]["context_cursor"] == "105.1"
    assert "message-2" in kwargs["agent_execution_context"]["rebuild_context"]
    assert "release notes" not in kwargs["unprocessed_messages"]
    # The judgment sees the same requests and correction as the agent.
    assert ctx.assessments[0]["unprocessed_messages"] == json.loads(
        kwargs["unprocessed_messages"]
    )
    assert store.load_channel_cursor("slack", "alice", "C1").processed_event_ids == [
        "E3",
        "E4",
        "E5",
    ]
    assert not store.is_processed_event("slack", "alice", "C1", "E6")
    assert not store.is_processed_event("slack", "alice", "C1", "E8")
    assert not store.is_processed_event("slack", "bob", "C1", "E5")


@pytest.mark.asyncio
async def test_failed_batch_is_refreshed_on_retry_without_losing_action_evidence(
    tmp_path,
):
    store = FileConversationStateStore(base_dir=tmp_path / "state")
    events = [_batch_event(3, mentions=["U_ALICE"]), _batch_event(5)]
    for event in events:
        store.upsert_pending_event("slack", "alice", "C1", event)
    service = SnapshotChatService(events)
    first = _batch_context("crash")
    with pytest.raises(RuntimeError, match="agent exited"):
        await chat_conversation_workflow.main(
            first, chat_service=service, state_store=store
        )
    assert store.load_channel_cursor("slack", "alice", "C1").processed_event_ids == []
    assert len(store.load_pending_events("slack", "alice", "C1")) == 2
    RunStore().append_evidence("batch-run", "chat_reaction", {"message_ts": "103.1"})
    events.append(_batch_event(7, text="Cancel deployment; review only"))
    retry = _batch_context(attempt=2)
    await chat_conversation_workflow.main(
        retry, chat_service=service, state_store=store
    )
    kwargs = _agent_invocations(retry)[0][1]
    assert kwargs["agent_execution_context"]["context_cursor"] == "107.1"
    assert "chat_reaction" in kwargs["previous_attempt_evidence"]
    assert [
        item["timestamp"] for item in json.loads(kwargs["unprocessed_messages"])
    ] == ["103.1", "105.1", "107.1"]
    evidence = RunStore().evidence("batch-run")
    assert any(item["evidence_type"] == "chat_reaction" for item in evidence)
    assert [
        item["payload"]["event_ids"]
        for item in evidence
        if item["evidence_type"] == "chat_batch"
    ] == [["E3", "E5"], ["E3", "E5", "E7"]]
    assert store.load_channel_cursor("slack", "alice", "C1").processed_event_ids == [
        "E3",
        "E5",
        "E7",
    ]


@pytest.mark.asyncio
async def test_completed_batch_recovery_does_not_consume_new_messages(
    tmp_path, monkeypatch
):
    store = FileConversationStateStore(base_dir=tmp_path / "state")
    events = [_batch_event(3, mentions=["U_ALICE"]), _batch_event(5)]
    service = SnapshotChatService(events)
    original_mark = store.mark_processed_events

    def crash_before_ack(*args):
        raise RuntimeError("interrupted before acknowledgement")

    monkeypatch.setattr(store, "mark_processed_events", crash_before_ack)
    with pytest.raises(RuntimeError, match="before acknowledgement"):
        await chat_conversation_workflow.main(
            _batch_context(), chat_service=service, state_store=store
        )
    assert RunStore().status("batch-run").completed
    monkeypatch.setattr(store, "mark_processed_events", original_mark)
    service.events.append(_batch_event(7, text="A new request after completion"))
    recovered = _batch_context("crash", attempt=2)
    await chat_conversation_workflow.main(
        recovered, chat_service=service, state_store=store
    )
    assert recovered.invocations == []
    assert store.load_channel_cursor("slack", "alice", "C1").processed_event_ids == [
        "E3",
        "E5",
    ]
    assert not store.is_processed_event("slack", "alice", "C1", "E7")


@pytest.mark.asyncio
async def test_batch_keeps_unread_messages_beyond_historical_context_bound(tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path / "state")
    events = [
        _batch_event(i, mentions=["U_ALICE"] if i == 3 else []) for i in range(3, 155)
    ]
    ctx = _batch_context()
    await chat_conversation_workflow.main(
        ctx, chat_service=SnapshotChatService(events), state_store=store
    )
    kwargs = _agent_invocations(ctx)[0][1]
    unread = json.loads(kwargs["unprocessed_messages"])
    assert len(unread) == len(events)
    assert unread[0]["timestamp"] == "103.1"
    assert unread[-1]["timestamp"] == "254.1"
    assert all(
        store.is_processed_event("slack", "alice", "C1", event.event_id)
        for event in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("history_limit", [2, 500])
async def test_dispatcher_consumes_one_batch_and_skips_its_queued_followers(
    monkeypatch, tmp_path, history_limit
):
    from guildbotics.drivers.execution import ExecutionCoordinator
    from guildbotics.drivers.pending_chat_dispatcher import PendingChatDispatcher

    store = FileConversationStateStore(base_dir=tmp_path / "state")
    events = [_batch_event(3, mentions=["U_ALICE"]), _batch_event(4), _batch_event(5)]
    for event in events:
        store.upsert_pending_event("slack", "alice", "C1", event)
    store = FileConversationStateStore(
        base_dir=tmp_path / "state", max_processed_events=history_limit
    )
    invocations = []
    service = SnapshotChatService(events)

    class Parent:
        logger = StubLogger()

        def clone_for(self, person):
            ctx = FakeInvokeContext("noop")
            invocations.append(ctx)

            async def close():
                pass

            ctx.aclose = close
            return ctx

    class Runner:
        def __init__(self, context, *_args):
            self.context = context

        async def run(self):
            await chat_conversation_workflow.main(
                self.context, chat_service=service, state_store=store
            )

    monkeypatch.setattr("guildbotics.drivers.workflow_dispatcher.CommandRunner", Runner)
    dispatcher = PendingChatDispatcher(
        Parent(), state_store=store, execution_coordinator=ExecutionCoordinator()
    )
    await dispatcher.process_person(
        Person(person_id="alice", name="Alice", is_active=True)
    )
    assert len(invocations) == 1
    assert len(_agent_invocations(invocations[0])) == 1
    assert store.load_pending_events("slack", "alice", "C1") == []


@pytest.mark.asyncio
async def test_batch_includes_members_own_reply_without_using_it_as_reaction_target(
    tmp_path,
):
    store = FileConversationStateStore(base_dir=tmp_path / "state")
    events = [_batch_event(3, mentions=["U_ALICE"]), _batch_event(5)]
    own_reply = _batch_event(6, text="I already finished the earlier work")
    own_reply.author_id = "U_ALICE"
    own_reply.is_bot_message = True
    events.append(own_reply)
    ctx = _batch_context()
    await chat_conversation_workflow.main(
        ctx, chat_service=SnapshotChatService(events), state_store=store
    )
    kwargs = _agent_invocations(ctx)[0][1]
    assert kwargs["message_ts"] == "105.1"
    assert kwargs["agent_execution_context"]["context_cursor"] == "106.1"
    assert "already finished" in kwargs["unprocessed_messages"]
    assert store.is_processed_event("slack", "alice", "C1", "E6")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "reply",
        "reaction",
        "noop",
        "blocked",
        "issue_comment",
        "git_publish",
        "git_push",
        "issue_update",
    ],
)
@pytest.mark.parametrize("recover", [False, True])
async def test_updates_read_during_turn_are_consumed_only_on_completion(
    monkeypatch, action, recover
):
    from guildbotics.capabilities.chat_updates import check_chat_updates
    from guildbotics.integrations.chat_receive_status import ChatReceiveStatus

    store = FileConversationStateStore()
    service = SnapshotChatService([_batch_event(3, mentions=["U_ALICE"])])
    secondary = action in {"issue_comment", "git_publish", "git_push", "issue_update"}
    handled = secondary or action in {"reply", "reaction"}
    ctx = _batch_context("noop" if secondary else action)
    original_invoke = ctx.invoke
    ChatReceiveStatus().save("slack", "alice", "C1", state="ready")

    async def invoke(name, **kwargs):
        if name == "functions/handle_chat_event":
            store.upsert_pending_event("slack", "alice", "C1", _batch_event(5))
            result = check_chat_updates("alice", kwargs["workflow_run_id"])
            assert [item["event_id"] for item in result["messages"]] == ["E5"]
            assert not store.is_processed_event("slack", "alice", "C1", "E5")
            if secondary:
                RunStore().append_evidence(
                    kwargs["workflow_run_id"], action, {"published": True}
                )
            # This later arrival was never delivered and must stay pending.
            store.upsert_pending_event("slack", "alice", "C1", _batch_event(6))
            if action == "blocked":
                ChatReceiveStatus().save("slack", "alice", "C1", state="unavailable")
                RunStore().complete_run(
                    kwargs["workflow_run_id"],
                    "blocked",
                    "Reception stopped",
                    subject_type="chat",
                    subject_id="slack:C1:100.1:E3",
                    person_id="alice",
                )
                return {"status": "blocked", "message": "Reception stopped"}
        return await original_invoke(name, **kwargs)

    ctx.invoke = invoke
    if recover:
        original_ack = store.mark_processed_events

        def interrupted(*args):
            raise RuntimeError("crash before ack")

        monkeypatch.setattr(store, "mark_processed_events", interrupted)
        with pytest.raises(RuntimeError, match="crash before ack"):
            await chat_conversation_workflow.main(
                ctx, chat_service=service, state_store=store
            )
        monkeypatch.setattr(store, "mark_processed_events", original_ack)
        ctx = _batch_context("crash", attempt=2)
    await chat_conversation_workflow.main(ctx, chat_service=service, state_store=store)
    assert store.is_processed_event("slack", "alice", "C1", "E5") is handled
    assert not store.is_processed_event("slack", "alice", "C1", "E6")
    assert [
        item.event.event_id
        for item in store.load_pending_events("slack", "alice", "C1")
    ] == (["E6"] if handled else ["E5", "E6"])
    if action in {"noop", "blocked"}:
        following = _batch_context("noop", run_id="following-run")
        _set_incoming_event(following, event_id="E5", message_ts="105.1")
        await chat_conversation_workflow.main(
            following, chat_service=service, state_store=store
        )
        assert len(_agent_invocations(following)) == 1
        assert store.is_processed_event("slack", "alice", "C1", "E5")
