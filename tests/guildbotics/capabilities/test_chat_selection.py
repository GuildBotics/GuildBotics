from __future__ import annotations

import json
import types
from uuid import uuid4

import pytest

from guildbotics.capabilities import chat_selection
from guildbotics.capabilities.chat_selection import (
    ChatAttempt,
    ChatSelector,
)
from guildbotics.capabilities.decisions.models import Selection
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.commands.agent_turn import run_agent_turn
from guildbotics.drivers.command_runner import HostRunLedger
from guildbotics.entities.team import Person, Role
from guildbotics.integrations.chat_state_store import (
    ThreadContextUnavailableError,
    ThreadConversationState,
    ThreadHandoffState,
    ThreadMessageState,
    ThreadSystemNoticeState,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.integrations.local import chat
from guildbotics.integrations.local.chat import LocalChatService
from guildbotics.intelligences.agent_runtime.models import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)
from guildbotics.intelligences.agent_runtime.wire import (
    CommandAccess,
)
from guildbotics.runtime.chat_service import (
    ChatEventPage,
    ChatIdentity,
)
from guildbotics.runtime.member_invocation import (
    ChatSubject,
    MemberInvocation,
    Work,
    member_invocation_scope,
)
from guildbotics.runtime.workflow_invocation import (
    WORKFLOW_INVOCATION_KEY,
    ChatTurn,
    WorkflowInvocation,
)
from guildbotics.templates.commands.workflows import chat_conversation_workflow
from guildbotics.utils.correlation import trace_scope
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.command_environment_doubles import runs_as
from tests.guildbotics.local_chat import at, chat_event, lines, position, say

_WORKFLOW = "workflows/chat_conversation_workflow"


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


ALICE = types.SimpleNamespace(person_id="alice", name="Alice")


class FakeChatService(LocalChatService):
    """Alice's local chat, which a test can make fail."""

    def __init__(self) -> None:
        super().__init__(ALICE)  # type: ignore[arg-type]
        self.fail_identity = False
        self.fail_post = False

    async def get_bot_identity(self) -> ChatIdentity:
        if self.fail_identity:
            raise RuntimeError("invalid_auth")
        return await super().get_bot_identity()

    async def post_message(self, channel_id, text, **kwargs):
        if self.fail_post:
            raise RuntimeError("is_archived")
        return await super().post_message(channel_id, text, **kwargs)

    @property
    def posts(self) -> list[tuple[str, str, str, dict | None]]:
        return [
            ("C1", line["text"], line["thread_id"], line.get("metadata"))
            for line in _channel()
            if line.get("author") == "alice" and "reaction" not in line
        ]

    @property
    def reactions(self) -> list[tuple[str, str, str]]:
        return [
            ("C1", line["message_id"], line["reaction"])
            for line in _channel()
            if "reaction" in line
        ]


def _channel() -> list[dict]:
    return lines("C1") if chat.channel_path("C1").exists() else []


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

    monkeypatch.setattr(chat_selection, "assess", assess)


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
            profile={"chat": {"subscriptions": [{"service": "local"}]}},
        )
        super().__init__(
            person=person,
            team=types.SimpleNamespace(members=[]),
            logger=StubLogger(),
            language_name="日本語",
            shared_state={},
        )
        self.action = action
        self.rate_limit_details = {
            "retry_after_at": "2026-07-04T11:44:00+09:00",
            "retry_after_text": "11:44 AM",
        }
        self.incoming: types.SimpleNamespace | None = None
        self.retry_context: dict | None = None
        self.logger.context = self
        self.assessments = []
        self.invocations: list[tuple[str, dict]] = []
        # When set, only the Nth handle_chat_event call records a completion, so
        # earlier attempts fail the gate and the host retries the turn.
        self.complete_on_attempt: int | None = None
        self._handle_calls = 0
        self.decision_reason = "request"
        self.response_effort = None
        # Stand-ins for Context.pipe and what each invoked command received as
        # its user message.
        self.pipe = ""
        self.consumed_messages: list[tuple[str, str]] = []

    async def invoke(self, name: str, /, **kwargs):
        # Mirror CommandRunner: a turn that names a completion budget is driven
        # by the host until the member records its completion.
        execution_context = kwargs.get("agent_execution_context")
        if isinstance(execution_context, dict) and execution_context.get(
            "max_completion_attempts"
        ):

            async def _turn(turn_context, parameters):
                return await self._invoke_once(
                    name,
                    **{
                        **kwargs,
                        **parameters,
                        "agent_execution_context": turn_context,
                    },
                )

            invocation = self.shared_state[WORKFLOW_INVOCATION_KEY]
            return await run_agent_turn(
                invoke=_turn,
                execution_context=execution_context,
                ledger=HostRunLedger(invocation.run_id, invocation.work),
            )
        return await self._invoke_once(name, **kwargs)

    @property
    def run_id(self) -> str:
        """The run the host started the workflow for."""
        return self.shared_state[WORKFLOW_INVOCATION_KEY].run_id

    def as_member(self):
        """Run what follows as the turn's member commands do: under the
        invocation of the workflow run's work."""
        invocation = self.shared_state[WORKFLOW_INVOCATION_KEY]
        return member_invocation_scope(
            MemberInvocation(run_id=invocation.run_id, work=invocation.work)
        )

    async def _invoke_once(self, name: str, /, **kwargs):
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
                    error_details=self.rate_limit_details,
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
        run_id = self.run_id
        store = RunStore()
        if self.action == "reply":
            store.append_evidence(
                run_id,
                "chat_reply",
                {
                    "service": "local",
                    "channel_id": kwargs["channel_id"],
                    "message_id": "200.1",
                    "thread_id": kwargs["thread_id"],
                    "occurred_at": at(200.1).isoformat(),
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
                    f"local:{kwargs['channel_id']}:"
                    f"{kwargs['thread_id']}:{kwargs['event_id']}"
                ),
                person_id=kwargs["person_id"],
            )
        elif self.action == "reaction":
            store.append_evidence(
                run_id,
                "chat_reaction",
                {
                    "service": "local",
                    "channel_id": kwargs["channel_id"],
                    "message_id": kwargs["message_id"],
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
                    f"local:{kwargs['channel_id']}:"
                    f"{kwargs['thread_id']}:{kwargs['event_id']}"
                ),
                person_id=kwargs["person_id"],
            )
        elif self.action == "noop":
            store.append_evidence(
                run_id,
                "chat_noop",
                {
                    "service": "local",
                    "channel_id": kwargs["channel_id"],
                    "thread_id": kwargs["thread_id"],
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
                    f"local:{kwargs['channel_id']}:"
                    f"{kwargs['thread_id']}:{kwargs['event_id']}"
                ),
                person_id=kwargs["person_id"],
            )
        return {"status": "done", "message": "done"}


def _set_incoming_event(
    ctx: types.SimpleNamespace,
    *,
    message_id: str = "100.1",
    text: str = "@alice please check",
    chat_participation: str = "strict",
    author: str = "user",
) -> None:
    """Have ``author`` write the event in thread ``100.1`` of the local
    channel, starting the thread first when it is not there."""
    if message_id != _ROOT and not any(
        line.get("message_id") == _ROOT for line in _channel()
    ):
        say("C1", "thread start", message_id=_ROOT, occurred_at=at(float(_ROOT)))
    event = say(
        "C1",
        text,
        author=author,
        message_id=message_id,
        thread_id=_ROOT,
        occurred_at=at(float(message_id)),
    )
    ctx.incoming = types.SimpleNamespace(
        service_name="local",
        channel_id="C1",
        event=event,
        chat_participation=chat_participation,
    )


_ROOT = "100.1"


async def _dispatch(ctx, *, chat_service, state_store) -> None:
    """Drive the queued event the way the dispatcher does: select, then respond."""
    incoming = ctx.incoming
    retry = ctx.retry_context or {}
    attempt = ChatAttempt(
        run_id=retry.get("run_id") or uuid4().hex,
        attempt_count=retry.get("attempt_count", 1),
        max_attempts=retry.get("max_attempts", 5),
    )
    selector = ChatSelector(
        ctx, command=_WORKFLOW, chat_service=chat_service, state_store=state_store
    )
    batch = await selector.prepare(
        service_name=incoming.service_name,
        channel_id=incoming.channel_id,
        event=incoming.event,
        chat_participation=incoming.chat_participation,
        run_id=attempt.run_id,
    )
    if batch is None:
        return

    async def run_turn(turn: ChatTurn) -> None:
        ctx.shared_state[WORKFLOW_INVOCATION_KEY] = WorkflowInvocation(
            command=_WORKFLOW,
            person_id="alice",
            source="event_queue",
            trigger_type="chat",
            payload=turn.model_dump(),
            run_id=attempt.run_id,
            work=Work.of_chat(turn.subject),
        )
        await chat_conversation_workflow.main(ctx)

    await selector.run(batch, attempt, run_turn)


@pytest.mark.asyncio
async def test_workflow_delegates_to_handle_chat_event_and_updates_reply_state(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(ctx)

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert service.posts == []
    assert service.reactions == []
    kwargs = _agent_invocations(ctx)[0][1]
    assert kwargs["person_id"] == "alice"
    assert kwargs["service_name"] == "local"
    assert kwargs["channel_id"] == "C1"
    execution_context = kwargs["agent_execution_context"]
    # The run and the work are the host's, fixed when it selected the event.
    assert ctx.shared_state[WORKFLOW_INVOCATION_KEY].work == Work.of_chat(
        ChatSubject("local", "C1", "100.1", "C1:100.1", "alice")
    )
    assert "run_id" not in execution_context
    assert "work_identity" not in execution_context
    assert execution_context["context_cursor"] == position(100.1)
    assert execution_context["event_id"] == "C1:100.1"
    assert execution_context["resume_policy"] == "auto"
    # The continuation prompt names the exact run/event and the missing
    # completion record, so a resumed session cannot mistake another run's
    # completion for this one.
    assert execution_context["continuation_input"] == t(
        "commands.workflows.common.agent_chat_continuation",
        run_id=ctx.run_id,
        event_id="C1:100.1",
    )
    assert ctx.run_id in execution_context["continuation_input"]
    assert "C1:100.1" in execution_context["continuation_input"]
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

    channel_state = state_store.load_channel_cursor("local", "alice", "C1")
    assert channel_state.processed_event_ids == ["C1:100.1"]
    thread_messages = state_store.load_thread_messages("local", "alice", "C1", "100.1")
    assert [message.message_id for message in thread_messages] == ["100.1", "200.1"]
    assert thread_messages[1].is_bot_message is True
    thread_state = state_store.load_thread_state("local", "alice", "C1", "100.1")
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
            "service": "local",
            "channel_id": "C1",
            "message_id": "200.1",
            "thread_id": "100.1",
            "occurred_at": at(200.1).isoformat(),
            "text": "確認します。",
            "posted": True,
        },
    )
    store.complete_run(
        run_id,
        "done",
        "Posted a reply.",
        subject_type="chat",
        subject_id="local:C1:100.1:C1:100.1",
        person_id="alice",
    )
    ctx.retry_context = {
        "attempt_count": 2,
        "max_attempts": 5,
        "is_final_attempt": False,
        "run_id": run_id,
    }

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    # A crash between the agent's completion record and the dispatcher marking
    # the event processed must resume from the recorded evidence: the agent is
    # never re-invoked (no duplicated replies/reactions) and the event still
    # terminalizes normally.
    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("local", "alice", "C1")
    assert channel_state.processed_event_ids == ["C1:100.1"]
    thread_messages = state_store.load_thread_messages("local", "alice", "C1", "100.1")
    assert [message.message_id for message in thread_messages] == ["100.1", "200.1"]
    assert thread_messages[1].is_bot_message is True
    thread_state = state_store.load_thread_state("local", "alice", "C1", "100.1")
    assert "alice" in thread_state.participants


@pytest.mark.asyncio
async def test_two_messages_in_one_thread_share_conversation_and_advance_cursor(
    tmp_path,
) -> None:
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    first = FakeInvokeContext("reply")
    first.response_effort = "high"
    _set_incoming_event(first, message_id="100.1")
    await _dispatch(first, chat_service=service, state_store=state_store)
    second = FakeInvokeContext("reply")
    second.response_effort = "default"
    _set_incoming_event(second, message_id="101.1")
    await _dispatch(second, chat_service=service, state_store=state_store)

    first_context = _agent_invocations(first)[0][1]["agent_execution_context"]
    second_context = _agent_invocations(second)[0][1]["agent_execution_context"]
    assert _agent_invocations(first)[0][1]["effort"] == "high"
    assert _agent_invocations(second)[0][1]["effort"] == "high"
    assert (
        first.shared_state[WORKFLOW_INVOCATION_KEY].work.identity
        == second.shared_state[WORKFLOW_INVOCATION_KEY].work.identity
    )
    assert first_context["context_cursor"] == position(100.1)
    assert second_context["context_cursor"] == position(101.1)
    assert second_context["rebuild_context_complete"] is True
    assert second_context["attempt"] == 1
    rebuilt = json.loads(second_context["rebuild_context"])
    assert len(rebuilt) == 1
    contents = [message["content"] for message in rebuilt]
    assert contents.count("@alice please check") == 1
    assert "確認します。" not in contents
    assert {message["timestamp"] for message in rebuilt} == {
        position(100.1),
    }


@pytest.mark.asyncio
async def test_live_thread_snapshot_paginates_and_keeps_latest_bound() -> None:
    class PaginatedChatService(FakeChatService):
        def __init__(self) -> None:
            super().__init__()
            self.cursors: list[str | None] = []

        async def list_thread_events(
            self, channel_id, *, thread_id, cursor=None, limit=100
        ) -> ChatEventPage:
            self.cursors.append(cursor)
            start, stop, next_cursor = (
                (1, 101, "page-2") if cursor is None else (101, 151, None)
            )
            return ChatEventPage(
                events=[
                    chat_event(
                        event_id=f"E{index}",
                        channel_id=channel_id,
                        message_id=f"{index}.1",
                        thread_id=thread_id,
                        author_id="user",
                        text=f"message-{index}",
                    )
                    for index in range(start, stop)
                ],
                cursor=next_cursor,
            )

    service = PaginatedChatService()
    context = FakeInvokeContext("noop")
    event = chat_event(
        event_id="E150",
        channel_id="C1",
        message_id="150.1",
        thread_id="1.1",
        author_id="user",
        text="message-150",
    )
    payload = await chat_selection._build_agent_prompt_payload(
        context=context,
        chat_service=service,
        event=event,
        thread_messages=[],
        self_user_id="alice",
        thread_state=ThreadConversationState(channel_id="C1", thread_id="1.1"),
        chat_participation="strict",
        live_thread=await chat_selection._fetch_thread_events(
            context=context, chat_service=service, event=event
        ),
        batch_events=[event],
    )

    assert service.cursors == [None, "page-2"]
    assert payload["thread_context_complete"] is True
    assert len(payload["thread_context"]) == 100
    assert payload["thread_context"][0]["timestamp"] == position(50.1)
    assert payload["thread_context"][-1]["timestamp"] == position(149.1)


@pytest.mark.asyncio
async def test_reaction_only_completion_processes_without_bot_message(
    tmp_path, monkeypatch
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reaction")
    _set_incoming_event(ctx)

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    channel_state = state_store.load_channel_cursor("local", "alice", "C1")
    assert channel_state.processed_event_ids == ["C1:100.1"]
    thread_messages = state_store.load_thread_messages("local", "alice", "C1", "100.1")
    assert [message.message_id for message in thread_messages] == ["100.1"]
    # A reaction is a visible action, so the member is recorded as a participant.
    thread_state = state_store.load_thread_state("local", "alice", "C1", "100.1")
    assert "alice" in thread_state.participants


@pytest.mark.asyncio
async def test_noop_completion_processes_without_visible_action(tmp_path, monkeypatch):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(ctx)

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    channel_state = state_store.load_channel_cursor("local", "alice", "C1")
    assert channel_state.processed_event_ids == ["C1:100.1"]
    # noop takes no visible action, so the member must not be recorded as a
    # thread participant.
    thread_state = state_store.load_thread_state("local", "alice", "C1", "100.1")
    assert "alice" not in thread_state.participants


@pytest.mark.asyncio
async def test_taking_a_batch_records_the_runs_start_under_its_trace(tmp_path):
    """The workflow, not the dispatcher, records the start of a chat run.

    The dispatcher only claimed the event; the record is created once the
    member takes the batch, mirroring the trace the dispatcher opened so the
    run names its thread on every device.
    """
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(ctx)
    _set_retry_context(ctx, run_id="run-chat")

    with trace_scope(
        "event_listener",
        trace_id="run-chat",
        person_id="alice",
        attributes={"event.provider": "local", "slack.channel": "C1"},
    ):
        await _dispatch(ctx, chat_service=service, state_store=state_store)

    records = list(RunStore().records())
    assert [record.run_id for record in records] == ["run-chat"]
    record = records[0]
    assert record.source == "event_listener"
    assert record.execution_mode == "autonomous"
    assert record.member_id == "alice"
    assert record.work_kind == "workflows/chat_conversation_workflow"
    assert record.work_identity == {
        "kind": "chat-event",
        "event_id": "C1:100.1",
        "service": "local",
        "channel_id": "C1",
    }
    assert record.attributes == {"event.provider": "local", "slack.channel": "C1"}
    assert record.result is not None


@pytest.mark.asyncio
async def test_declined_batch_leaves_no_run_record(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(ctx, text="please check")
    _set_retry_context(ctx, run_id="run-declined")

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert ctx.invocations == []
    assert list(RunStore().records()) == []


@pytest.mark.asyncio
async def test_unshared_run_start_hands_the_batch_back(tmp_path, monkeypatch):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(ctx)
    _set_retry_context(ctx, run_id="run-unshared")
    monkeypatch.setattr(chat_selection, "await_shared_change", lambda change: False)

    with pytest.raises(ThreadContextUnavailableError):
        await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert ctx.invocations == []


def _set_retry_context(ctx: types.SimpleNamespace, *, run_id: str) -> None:
    ctx.retry_context = {
        "attempt_count": 1,
        "max_attempts": 5,
        "is_final_attempt": False,
        "run_id": run_id,
    }


@pytest.mark.asyncio
async def test_unmentioned_new_thread_is_processed_without_agent(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(ctx, text="please check")

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("local", "alice", "C1")
    assert channel_state.processed_event_ids == ["C1:100.1"]
    thread_messages = state_store.load_thread_messages("local", "alice", "C1", "100.1")
    assert thread_messages == []


@pytest.mark.asyncio
async def test_social_unmentioned_new_thread_delegates(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(
        ctx,
        text="今日のランチどうします?",
        chat_participation="social",
    )

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    kwargs = _agent_invocations(ctx)[0][1]
    assert kwargs["chat_participation"] == "social"
    channel_state = state_store.load_channel_cursor("local", "alice", "C1")
    assert channel_state.processed_event_ids == ["C1:100.1"]


@pytest.mark.asyncio
async def test_unmentioned_followup_after_prior_mention_delegates(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    state_store.append_thread_message(
        "local",
        "alice",
        "C1",
        "100.1",
        ThreadMessageState(
            channel_id="C1",
            thread_id="100.1",
            message_id="100.1",
            occurred_at=at(100.1),
            author_id="user",
            text="@alice please check",
            mentions=["alice"],
        ),
    )
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(ctx, message_id="100.2", text="Any update?")

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    kwargs = _agent_invocations(ctx)[0][1]
    assert kwargs["event_id"] == "C1:100.2"
    assert kwargs["message_id"] == "100.2"


@pytest.mark.asyncio
async def test_muted_unmentioned_followup_after_prior_mention_skips(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    state_store.append_thread_message(
        "local",
        "alice",
        "C1",
        "100.1",
        ThreadMessageState(
            channel_id="C1",
            thread_id="100.1",
            message_id="100.1",
            occurred_at=at(100.1),
            author_id="user",
            text="@alice please check",
            mentions=["alice"],
        ),
    )
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(
        ctx,
        message_id="100.2",
        text="Any update?",
        chat_participation="muted",
    )

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("local", "alice", "C1")
    assert channel_state.processed_event_ids == ["C1:100.2"]


class MentionSnapshotChatService(FakeChatService):
    """Serves a thread snapshot whose first message mentions the member."""

    async def list_thread_events(
        self, channel_id, *, thread_id, cursor=None, limit=100
    ) -> ChatEventPage:
        return ChatEventPage(
            events=[
                chat_event(
                    event_id="C1:100.1",
                    channel_id=channel_id,
                    message_id="100.1",
                    thread_id=thread_id,
                    author_id="user",
                    text="@alice please check",
                    mentions=["alice"],
                )
            ]
        )


class UnavailableChatService(FakeChatService):
    async def list_thread_events(
        self, channel_id, *, thread_id, cursor=None, limit=100
    ) -> ChatEventPage:
        raise RuntimeError("provider unavailable")


@pytest.mark.asyncio
async def test_empty_cache_followup_participates_via_provider_snapshot(tmp_path):
    # Handoff to a device with an empty local chat cache: the provider snapshot
    # alone must yield the same participation decision.
    service = MentionSnapshotChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(ctx, message_id="100.2", text="Any update?")

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    kwargs = _agent_invocations(ctx)[0][1]
    assert kwargs["event_id"] == "C1:100.2"


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
    _set_incoming_event(ctx, message_id="100.2", text="Any update?")

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert service.fetch_count == 1
    cached = state_store.load_thread_messages("local", "alice", "C1", "100.1")
    assert [message.message_id for message in cached] == ["100.1", "100.2"]
    assert cached[0].mentions == ["alice"]


@pytest.mark.asyncio
async def test_provider_unavailable_without_cache_keeps_event_unprocessed(tmp_path):
    # Neither the provider nor the local cache can decide participation: the
    # event must bubble as ThreadContextUnavailableError, staying unprocessed
    # and creating no task run.
    service = UnavailableChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(ctx, message_id="100.2", text="Any update?")

    with pytest.raises(ThreadContextUnavailableError):
        await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("local", "alice", "C1")
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
        "local",
        "alice",
        "C1",
        "100.1",
        ThreadMessageState(
            channel_id="C1",
            thread_id="100.1",
            message_id="100.1",
            occurred_at=at(100.1),
            author_id="user",
            text="@alice please check",
            mentions=["alice"],
        ),
    )
    ctx = FakeInvokeContext("noop")
    _set_incoming_event(
        ctx,
        message_id="100.2",
        text="Any update?",
        chat_participation=participation,
    )

    with pytest.raises(ThreadContextUnavailableError):
        await _dispatch(ctx, chat_service=service, state_store=state_store)
    assert ctx.invocations == []
    assert not state_store.is_processed_event("local", "alice", "C1", "C1:100.2")


@pytest.mark.asyncio
async def test_followup_mentioning_other_member_skips_even_after_prior_mention(
    tmp_path,
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    state_store.append_thread_message(
        "local",
        "alice",
        "C1",
        "100.1",
        ThreadMessageState(
            channel_id="C1",
            thread_id="100.1",
            message_id="100.1",
            occurred_at=at(100.1),
            author_id="user",
            text="@alice please check",
            mentions=["alice"],
        ),
    )
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(
        ctx,
        message_id="100.2",
        text="@bob can you check?",
    )

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("local", "alice", "C1")
    assert channel_state.processed_event_ids == ["C1:100.2"]


def test_author_labels_include_mentionable_team_members_not_in_thread():
    ctx = types.SimpleNamespace(person=types.SimpleNamespace(person_id="alice"))
    labels = chat_selection._build_author_labels(
        ctx,
        "alice",
        chat_event(
            event_id="C1:100.1",
            channel_id="C1",
            message_id="100.1",
            thread_id="100.1",
            author_id="user",
            text="please check",
        ),
        [],
        {"alice": "alice", "bob": "bob"},
    )

    assert labels["alice"] == "alice"
    assert labels["bob"] == "bob"


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

    candidates = chat_selection._build_handoff_candidates(
        ctx, {"alice": "alice", "bob": "bob"}
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
async def test_chat_user_to_person_labels_uses_each_members_chat_user():
    alice = Person(person_id="alice", name="Alice")
    bob = Person(person_id="bob", name="Bob", person_type="human")
    ctx = types.SimpleNamespace(
        team=types.SimpleNamespace(members=[alice, bob]),
        clone_for=lambda member: pytest.fail(member.person_id),
    )

    labels = await chat_selection._chat_user_to_person_labels(ctx, FakeChatService())

    assert labels == {"alice": "alice", "bob": "bob"}


@pytest.mark.asyncio
async def test_an_agent_without_a_configured_chat_user_is_its_credentials_user():
    class Unconfigured(FakeChatService):
        def self_user_id(self, person) -> str:
            return ""

    alice = Person(person_id="alice", name="Alice")
    bob = Person(person_id="bob", name="Bob", person_type="human")
    closed: list[str] = []

    def clone_for(member):
        async def aclose() -> None:
            closed.append(member.person_id)

        return types.SimpleNamespace(
            get_chat_service=lambda: LocalChatService(member), aclose=aclose
        )

    ctx = types.SimpleNamespace(
        team=types.SimpleNamespace(members=[alice, bob]), clone_for=clone_for
    )

    labels = await chat_selection._chat_user_to_person_labels(ctx, Unconfigured())

    # A human has no credential of their own to ask.
    assert labels == {"alice": "alice"}
    assert closed == ["alice"]


def test_record_handoffs_saves_mentions_to_known_members():
    alice = Person(person_id="alice", name="Alice")
    bob = Person(
        person_id="bob",
        name="Bob",
        roles={"design": Role(id="design", summary="Design", description="")},
    )
    ctx = types.SimpleNamespace(team=types.SimpleNamespace(members=[alice, bob]))
    thread_state = ThreadConversationState(channel_id="C1", thread_id="100.1")

    chat_selection._record_handoffs(
        context=ctx,
        thread_state=thread_state,
        participant_labels={"alice": "alice", "bob": "bob"},
        mentioned_user_ids=["bob"],
        source_person_id="alice",
        message_id="200.1",
        text="@bob design観点を見てもらえますか?",
    )

    assert len(thread_state.handoffs) == 1
    handoff = thread_state.handoffs[0]
    assert handoff.person_id == "bob"
    assert handoff.roles == ["design"]
    assert handoff.message_id == "200.1"
    assert handoff.text == "@bob design観点を見てもらえますか?"


def test_record_handoffs_ignores_non_team_participant_labels():
    alice = Person(person_id="alice", name="Alice")
    ctx = types.SimpleNamespace(team=types.SimpleNamespace(members=[alice]))
    thread_state = ThreadConversationState(channel_id="C1", thread_id="100.1")

    chat_selection._record_handoffs(
        context=ctx,
        thread_state=thread_state,
        participant_labels={"alice": "alice", "user": "user_1"},
        mentioned_user_ids=["user"],
        source_person_id="alice",
        message_id="200.1",
        text="@user どう思いますか?",
    )

    assert thread_state.handoffs == []


@pytest.mark.asyncio
async def test_prompt_payload_includes_existing_handoffs():
    thread_state = ThreadConversationState(
        channel_id="C1",
        thread_id="100.1",
        handoffs=[
            ThreadHandoffState(
                person_id="bob",
                roles=["design"],
                message_id="200.1",
                text="@bob design観点を見てもらえますか?",
            )
        ],
    )
    ctx = FakeInvokeContext("noop")

    event = chat_event(
        event_id="E2",
        channel_id="C1",
        message_id="201.1",
        thread_id="100.1",
        author_id="user",
        text="Any thoughts?",
    )
    payload = await chat_selection._build_agent_prompt_payload(
        context=ctx,
        chat_service=FakeChatService(),
        event=event,
        thread_messages=[],
        self_user_id="alice",
        thread_state=thread_state,
        chat_participation="strict",
        live_thread=([], True),
        batch_events=[event],
    )

    assert payload["previous_thread_context"]["handoffs"] == [
        {
            "person_id": "bob",
            "roles": ["design"],
            "message_id": "200.1",
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
    ctx.retry_context = {
        "attempt_count": 3,
        "max_attempts": 3,
        "is_final_attempt": True,
        "run_id": "run-1",
    }

    # A single dispatch retries the agent in-process up to the budget, then
    # escalates and stops (no exception bubbles out).
    await _dispatch(ctx, chat_service=service, state_store=state_store)

    handle_calls = [
        kwargs
        for name, kwargs in ctx.invocations
        if name == "functions/handle_chat_event"
    ]
    assert len(handle_calls) == 2
    # All attempts are turns of one run doing one work: one conversation.
    assert ctx.shared_state[WORKFLOW_INVOCATION_KEY].work.identity == (
        "local:alice:C1:100.1"
    )
    assert [call["agent_execution_context"]["attempt"] for call in handle_calls] == [
        3,
        4,
    ]

    # Escalated to the thread and stopped (event marked processed, no re-dispatch).
    assert state_store.load_channel_cursor(
        "local", "alice", "C1"
    ).processed_event_ids == ["C1:100.1"]
    assert len(service.posts) == 1
    channel_id, text, thread_id, metadata = service.posts[0]
    assert channel_id == "C1"
    assert thread_id == "100.1"
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
    ctx.retry_context = {
        "attempt_count": 2,
        "max_attempts": 2,
        "is_final_attempt": True,
        "run_id": "run-1",
    }

    # A failing agent run must be bounded and escalated, NOT raise out of the
    # workflow (which would leave the event queued for infinite retry).
    await _dispatch(ctx, chat_service=service, state_store=state_store)

    handle_calls = [
        kwargs
        for name, kwargs in ctx.invocations
        if name == "functions/handle_chat_event"
    ]
    assert len(handle_calls) == 1  # invoke exceptions are left to pending backoff
    assert len(service.posts) == 1  # escalated to the thread
    assert state_store.load_channel_cursor(
        "local", "alice", "C1"
    ).processed_event_ids == ["C1:100.1"]


@pytest.mark.asyncio
async def test_non_final_agent_run_failure_bubbles_for_pending_backoff(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("crash")
    _set_incoming_event(ctx)
    ctx.retry_context = {
        "attempt_count": 1,
        "max_attempts": 5,
        "is_final_attempt": False,
        "run_id": "run-1",
    }

    with pytest.raises(RuntimeError, match="agent exited non-zero"):
        await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert service.posts == []
    assert (
        state_store.load_channel_cursor("local", "alice", "C1").processed_event_ids
        == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("notice_language", ["en", "ja"], indirect=True)
@pytest.mark.parametrize(
    "at, hint, suffix",
    [
        ("2026-07-04T02:44:00Z", "11:44 AM", "_with_reset"),
        ("", "Resets in 1h", "_with_hint"),
        ("", "", ""),
    ],
)
async def test_rate_limit_posts_notice_and_leaves_event_pending(
    tmp_path, notice_language, at, hint, suffix, local_rate_limit_timezone
):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("rate_limit")
    ctx.rate_limit_details = {"retry_after_at": at, "retry_after_text": hint}
    _set_incoming_event(ctx)

    with pytest.raises(CliAgentExecutionError):
        await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert len(service.posts) == 1
    _channel_id, text, _thread_ts, metadata = service.posts[0]
    assert text == t(
        f"commands.workflows.common.rate_limited_escalation{suffix}",
        retry_after="2026-07-04 11:44:00+09:00" if at else hint,
        retry_guidance=t("commands.workflows.common.rate_limited_retry_chat"),
    )
    assert metadata is not None
    payload = metadata["event_payload"]
    assert payload["reason"] == "rate_limited"
    assert payload["routing"] == "suppress"
    assert payload.get("retry_after_at", "") == at
    assert payload.get("retry_after_text", "") == hint
    assert (
        state_store.load_channel_cursor("local", "alice", "C1").processed_event_ids
        == []
    )
    thread_state = state_store.load_thread_state("local", "alice", "C1", "100.1")
    assert len(thread_state.system_notices) == 1
    assert thread_state.system_notices[0].reason == "rate_limited"


@pytest.mark.asyncio
async def test_duplicate_system_notice_is_not_posted_again(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    state = ThreadConversationState(channel_id="C1", thread_id="100.1")
    state.system_notices.append(
        ThreadSystemNoticeState(
            kind="workflow_error",
            reason="failed",
            person_id="alice",
            source_event_id="C1:100.1",
            message_id="300.1",
        )
    )
    state_store.save_thread_state("local", "alice", "C1", "100.1", state)
    ctx = FakeInvokeContext("crash")
    _set_incoming_event(ctx)
    ctx.retry_context = {
        "attempt_count": 5,
        "max_attempts": 5,
        "is_final_attempt": True,
        "run_id": "run-1",
    }

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert service.posts == []
    assert state_store.load_channel_cursor(
        "local", "alice", "C1"
    ).processed_event_ids == ["C1:100.1"]


@pytest.mark.asyncio
async def test_final_notice_post_failure_marks_processed_without_notice_state(tmp_path):
    service = FakeChatService()
    service.fail_post = True
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("crash")
    _set_incoming_event(ctx)
    ctx.retry_context = {
        "attempt_count": 5,
        "max_attempts": 5,
        "is_final_attempt": True,
        "run_id": "run-1",
    }

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert service.posts == []
    assert state_store.load_channel_cursor(
        "local", "alice", "C1"
    ).processed_event_ids == ["C1:100.1"]
    thread_state = state_store.load_thread_state("local", "alice", "C1", "100.1")
    assert thread_state.system_notices == []


@pytest.mark.asyncio
async def test_non_final_prerun_identity_failure_bubbles(tmp_path):
    service = FakeChatService()
    service.fail_identity = True
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(ctx)
    ctx.retry_context = {
        "attempt_count": 1,
        "max_attempts": 5,
        "is_final_attempt": False,
        "run_id": "run-1",
    }

    with pytest.raises(RuntimeError, match="invalid_auth"):
        await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert (
        state_store.load_channel_cursor("local", "alice", "C1").processed_event_ids
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

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    handle_calls = [
        kwargs
        for name, kwargs in ctx.invocations
        if name == "functions/handle_chat_event"
    ]
    # Stops as soon as a turn records a terminal completion: no extra retries.
    assert len(handle_calls) == 2
    assert [call["agent_execution_context"]["attempt"] for call in handle_calls] == [
        1,
        2,
    ]
    assert service.posts == []  # no escalation; it completed
    assert state_store.load_channel_cursor(
        "local", "alice", "C1"
    ).processed_event_ids == ["C1:100.1"]


@pytest.mark.asyncio
async def test_obvious_self_message_is_marked_processed_without_agent(tmp_path):
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("reply")
    _set_incoming_event(ctx, text="bot message", author="alice")

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert ctx.invocations == []
    channel_state = state_store.load_channel_cursor("local", "alice", "C1")
    assert channel_state.processed_event_ids == ["C1:100.1"]


@pytest.mark.asyncio
async def test_final_attempt_abandon_records_dispatch_abandoned_event(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GUILDBOTICS_CHAT_MAX_ATTEMPTS", "3")
    service = FakeChatService()
    state_store = FileConversationStateStore(base_dir=tmp_path)
    ctx = FakeInvokeContext("missing")
    _set_incoming_event(ctx)
    ctx.retry_context = {
        "attempt_count": 3,
        "max_attempts": 3,
        "is_final_attempt": True,
        "run_id": "run-1",
    }
    recorded: list[dict] = []
    monkeypatch.setattr(
        chat_selection,
        "record_chat_dispatch_abandoned",
        lambda **kwargs: recorded.append(kwargs),
    )

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert len(recorded) == 1
    assert recorded[0]["event_id"] == "C1:100.1"
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
            "service": "local",
            "channel_id": "C1",
            "message_id": "200.1",
            "thread_id": "100.1",
            "occurred_at": at(200.1).isoformat(),
            "text": "確認します。",
            "posted": True,
        },
    )
    store.complete_run(
        run_id,
        "done",
        "Posted a reply.",
        subject_type="chat",
        subject_id="local:C1:100.1:C1:100.1",
        person_id="alice",
    )
    ctx.retry_context = {
        "attempt_count": 2,
        "max_attempts": 5,
        "is_final_attempt": False,
        "run_id": run_id,
    }
    recorded: list[dict] = []
    monkeypatch.setattr(
        chat_selection,
        "record_workflow_completed",
        lambda **kwargs: recorded.append(kwargs),
    )

    await _dispatch(ctx, chat_service=service, state_store=state_store)

    assert recorded == [{"run_id": run_id, "attempt": 2, "recovered": True}]


# --------------------------------------------------------------------------- #
# Judgment: delegation and retries
# --------------------------------------------------------------------------- #


async def _run_chat_event(tmp_path, monkeypatch, ctx, state_store) -> FakeChatService:
    service = FakeChatService()
    _set_incoming_event(ctx)
    await _dispatch(ctx, chat_service=service, state_store=state_store)
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
    state = ThreadConversationState(channel_id="C1", thread_id="100.1", effort=stored)
    state_store.save_thread_state("local", "alice", "C1", "100.1", state)
    ctx = FakeInvokeContext("reply")
    ctx.response_effort = candidate
    await _run_chat_event(tmp_path, monkeypatch, ctx, state_store)
    invocation = _agent_invocations(ctx)[0][1]
    assert invocation.get("effort", "") == expected
    assert "model" not in invocation and "brain" not in invocation
    assert (
        state_store.load_thread_state("local", "alice", "C1", "100.1").effort
        == expected
    )


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
    # The first agent attempt records no completion, so the host retries it.
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
        self, channel_id, *, thread_id, cursor=None, limit=100
    ):
        start = int(cursor or 0)
        end = start + limit
        return ChatEventPage(
            events=self.events[start:end],
            cursor=str(end) if end < len(self.events) else None,
        )


def _batch_event(number, *, text=None, mentions=(), thread_id="100.1"):
    return chat_event(
        channel_id="C1",
        message_id=f"{100 + number}.1",
        thread_id=thread_id,
        author_id="user",
        text=text or f"message-{number}",
        mentions=list(mentions),
        is_thread_reply=True,
    )


def _batch_context(action="noop", *, run_id="batch-run", attempt=1):
    ctx = FakeInvokeContext(action)
    _set_incoming_event(ctx, message_id="103.1")
    ctx.retry_context = {
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
        _batch_event(3, text="Deploy version A", mentions=["alice"]),
        _batch_event(4, text="Version B fixes the bug", mentions=["bob"]),
        _batch_event(5, text="Correction: deploy version B instead"),
    ]
    for event in (events[1], events[-1]):
        store.upsert_pending_event("local", "alice", "C1", event, participation)
    store.upsert_pending_event("local", "bob", "C1", events[-1])
    store.upsert_pending_event(
        "local", "alice", "C1", _batch_event(8, thread_id="other")
    )
    service = SnapshotChatService(events)
    ctx = _batch_context()
    ctx.incoming.chat_participation = participation
    original_invoke = ctx.invoke

    async def invoke(name, **kwargs):
        if name == "functions/handle_chat_event":
            new_event = _batch_event(6, text="Also update the release notes")
            service.events.append(new_event)
            store.upsert_pending_event("local", "alice", "C1", new_event)
        return await original_invoke(name, **kwargs)

    ctx.invoke = invoke
    await _dispatch(ctx, chat_service=service, state_store=store)

    assert len(_agent_invocations(ctx)) == 1
    kwargs = _agent_invocations(ctx)[0][1]
    unread = json.loads(kwargs["unprocessed_messages"])
    assert [item["content"] for item in unread] == [item.text for item in events[1:4]]
    assert kwargs["agent_execution_context"]["context_cursor"] == position(105.1)
    assert "message-2" in kwargs["agent_execution_context"]["rebuild_context"]
    assert "release notes" not in kwargs["unprocessed_messages"]
    # The judgment sees the same requests and correction as the agent.
    assert ctx.assessments[0]["unprocessed_messages"] == json.loads(
        kwargs["unprocessed_messages"]
    )
    assert store.load_channel_cursor("local", "alice", "C1").processed_event_ids == [
        "C1:103.1",
        "C1:104.1",
        "C1:105.1",
    ]
    assert not store.is_processed_event("local", "alice", "C1", "C1:106.1")
    assert not store.is_processed_event("local", "alice", "C1", "C1:108.1")
    assert not store.is_processed_event("local", "bob", "C1", "C1:105.1")


@pytest.mark.asyncio
async def test_failed_batch_is_refreshed_on_retry_without_losing_action_evidence(
    tmp_path,
):
    store = FileConversationStateStore(base_dir=tmp_path / "state")
    events = [_batch_event(3, mentions=["alice"]), _batch_event(5)]
    for event in events:
        store.upsert_pending_event("local", "alice", "C1", event)
    service = SnapshotChatService(events)
    first = _batch_context("crash")
    with pytest.raises(RuntimeError, match="agent exited"):
        await _dispatch(first, chat_service=service, state_store=store)
    assert store.load_channel_cursor("local", "alice", "C1").processed_event_ids == []
    assert len(store.load_pending_events("local", "alice", "C1")) == 2
    RunStore().append_evidence("batch-run", "chat_reaction", {"message_id": "103.1"})
    events.append(_batch_event(7, text="Cancel deployment; review only"))
    retry = _batch_context(attempt=2)
    await _dispatch(retry, chat_service=service, state_store=store)
    kwargs = _agent_invocations(retry)[0][1]
    assert kwargs["agent_execution_context"]["context_cursor"] == position(107.1)
    assert "chat_reaction" in kwargs["previous_attempt_evidence"]
    assert [
        item["timestamp"] for item in json.loads(kwargs["unprocessed_messages"])
    ] == [position(103.1), position(105.1), position(107.1)]
    evidence = RunStore().evidence("batch-run")
    assert any(item["evidence_type"] == "chat_reaction" for item in evidence)
    assert [
        item["payload"]["event_ids"]
        for item in evidence
        if item["evidence_type"] == "chat_batch"
    ] == [["C1:103.1", "C1:105.1"], ["C1:103.1", "C1:105.1", "C1:107.1"]]
    assert store.load_channel_cursor("local", "alice", "C1").processed_event_ids == [
        "C1:103.1",
        "C1:105.1",
        "C1:107.1",
    ]


@pytest.mark.asyncio
async def test_completed_batch_recovery_does_not_consume_new_messages(
    tmp_path, monkeypatch
):
    store = FileConversationStateStore(base_dir=tmp_path / "state")
    events = [_batch_event(3, mentions=["alice"]), _batch_event(5)]
    service = SnapshotChatService(events)
    original_mark = store.mark_processed_events

    def crash_before_ack(*args):
        raise RuntimeError("interrupted before acknowledgement")

    monkeypatch.setattr(store, "mark_processed_events", crash_before_ack)
    with pytest.raises(RuntimeError, match="before acknowledgement"):
        await _dispatch(_batch_context(), chat_service=service, state_store=store)
    assert RunStore().status("batch-run").completed
    monkeypatch.setattr(store, "mark_processed_events", original_mark)
    service.events.append(_batch_event(7, text="A new request after completion"))
    recovered = _batch_context("crash", attempt=2)
    await _dispatch(recovered, chat_service=service, state_store=store)
    assert recovered.invocations == []
    assert store.load_channel_cursor("local", "alice", "C1").processed_event_ids == [
        "C1:103.1",
        "C1:105.1",
    ]
    assert not store.is_processed_event("local", "alice", "C1", "C1:107.1")


@pytest.mark.asyncio
async def test_batch_keeps_unread_messages_beyond_historical_context_bound(tmp_path):
    store = FileConversationStateStore(base_dir=tmp_path / "state")
    events = [
        _batch_event(i, mentions=["alice"] if i == 3 else []) for i in range(3, 155)
    ]
    ctx = _batch_context()
    await _dispatch(ctx, chat_service=SnapshotChatService(events), state_store=store)
    kwargs = _agent_invocations(ctx)[0][1]
    unread = json.loads(kwargs["unprocessed_messages"])
    assert len(unread) == len(events)
    assert unread[0]["timestamp"] == position(103.1)
    assert unread[-1]["timestamp"] == position(254.1)
    assert all(
        store.is_processed_event("local", "alice", "C1", event.event_id)
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
    events = [_batch_event(3, mentions=["alice"]), _batch_event(4), _batch_event(5)]
    for event in events:
        store.upsert_pending_event("local", "alice", "C1", event)
    store = FileConversationStateStore(
        base_dir=tmp_path / "state", max_processed_events=history_limit
    )
    invocations = []
    service = SnapshotChatService(events)

    class Parent:
        logger = StubLogger()

        def clone_for(self, person):
            ctx = FakeInvokeContext("noop")
            ctx.get_chat_service = lambda: service
            invocations.append(ctx)

            async def close():
                pass

            ctx.aclose = close
            return ctx

    class Runner:
        access = CommandAccess()

        def __init__(self, context, _command, _args, cwd):
            self.cwd = cwd
            self.context = context

        async def run(self):
            await chat_conversation_workflow.main(self.context)

    runs_as(monkeypatch, Runner)
    dispatcher = PendingChatDispatcher(
        Parent(), state_store=store, execution_coordinator=ExecutionCoordinator()
    )
    await dispatcher.process_person(
        Person(person_id="alice", name="Alice", is_active=True)
    )
    # One context selects the batch, one runs its single agent turn.
    assert len(invocations) == 2
    assert len(_agent_invocations(invocations[1])) == 1
    assert store.load_pending_events("local", "alice", "C1") == []


@pytest.mark.asyncio
async def test_batch_includes_members_own_reply_without_using_it_as_reaction_target(
    tmp_path,
):
    store = FileConversationStateStore(base_dir=tmp_path / "state")
    events = [_batch_event(3, mentions=["alice"]), _batch_event(5)]
    own_reply = _batch_event(6, text="I already finished the earlier work")
    own_reply.author_id = "alice"
    own_reply.is_bot_message = True
    events.append(own_reply)
    ctx = _batch_context()
    await _dispatch(ctx, chat_service=SnapshotChatService(events), state_store=store)
    kwargs = _agent_invocations(ctx)[0][1]
    assert kwargs["message_id"] == "105.1"
    assert kwargs["agent_execution_context"]["context_cursor"] == position(106.1)
    assert "already finished" in kwargs["unprocessed_messages"]
    assert store.is_processed_event("local", "alice", "C1", "C1:106.1")


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
    service = SnapshotChatService([_batch_event(3, mentions=["alice"])])
    secondary = action in {"issue_comment", "git_publish", "git_push", "issue_update"}
    handled = secondary or action in {"reply", "reaction"}
    ctx = _batch_context("noop" if secondary else action)
    original_invoke = ctx.invoke
    ChatReceiveStatus().save("local", "alice", "C1", state="ready")

    async def invoke(name, **kwargs):
        if name == "functions/handle_chat_event":
            store.upsert_pending_event("local", "alice", "C1", _batch_event(5))
            with ctx.as_member():
                result = check_chat_updates("alice")
            assert [item["event_id"] for item in result["messages"]] == ["C1:105.1"]
            assert not store.is_processed_event("local", "alice", "C1", "C1:105.1")
            if secondary:
                RunStore().append_evidence(
                    ctx.run_id,
                    action,
                    {"published": True},
                )
            # This later arrival was never delivered and must stay pending.
            store.upsert_pending_event("local", "alice", "C1", _batch_event(6))
            if action == "blocked":
                ChatReceiveStatus().save("local", "alice", "C1", state="unavailable")
                RunStore().complete_run(
                    ctx.run_id,
                    "blocked",
                    "Reception stopped",
                    subject_type="chat",
                    subject_id="local:C1:100.1:C1:103.1",
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
            await _dispatch(ctx, chat_service=service, state_store=store)
        monkeypatch.setattr(store, "mark_processed_events", original_ack)
        ctx = _batch_context("crash", attempt=2)
    await _dispatch(ctx, chat_service=service, state_store=store)
    assert store.is_processed_event("local", "alice", "C1", "C1:105.1") is handled
    assert not store.is_processed_event("local", "alice", "C1", "C1:106.1")
    assert [
        item.event.event_id
        for item in store.load_pending_events("local", "alice", "C1")
    ] == (["C1:106.1"] if handled else ["C1:105.1", "C1:106.1"])
    if action in {"noop", "blocked"}:
        following = _batch_context("noop", run_id="following-run")
        _set_incoming_event(following, message_id="105.1")
        await _dispatch(following, chat_service=service, state_store=store)
        assert len(_agent_invocations(following)) == 1
        assert store.is_processed_event("local", "alice", "C1", "C1:105.1")
