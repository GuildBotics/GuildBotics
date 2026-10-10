from __future__ import annotations

import asyncio
import json

import pytest

from guildbotics.environment import diagnostics
from guildbotics.environment.store import ConversationStore
from guildbotics.guest import cli_agent
from guildbotics.intelligences import cli_agents
from guildbotics.intelligences.agent_runtime import models as agent_models
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentEventKind,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    AgentTerminalResult,
    ConversationKey,
    ResumePolicy,
)
from tests.guildbotics.environment.window_doubles import (
    WindowDouble,
    enter_command,
)
from tests.guildbotics.local_chat import position
from tests.guildbotics.slot_mappings import use_cli_agent_slots


@pytest.fixture
def in_a_command(monkeypatch, tmp_path) -> WindowDouble:
    """The brain's turns run inside a command's environment, as every turn
    does, whose window answers the conversations from the test's workspace;
    the adapter they speak through is the test's own."""
    window = WindowDouble(tmp_path)
    enter_command(monkeypatch, window)
    return window


@pytest.fixture
def doing(monkeypatch, in_a_command: WindowDouble):
    """Make the command's grant hold its turns to the work of ``work_kind``
    and ``work_identity`` in ``run_id``, as the host settles it."""

    def enter(work_kind: str, work_identity: str, run_id: str = "run-1") -> None:
        enter_command(
            monkeypatch,
            in_a_command,
            run_id=run_id,
            work_kind=work_kind,
            work_identity=work_identity,
        )

    return enter


class _Logger:
    debug = info = warning = error = lambda *args, **kwargs: None


class _Adapter:
    name = "codex-app-server"

    def __init__(self) -> None:
        self.fail = False
        self.prompts: list[str] = []
        self.contexts = []
        self.model = ""
        self.effort = ""

    async def close(self) -> None:
        self.closed = True

    async def run_turn(self, prompt, context, conversation, emit):
        self.prompts.append(prompt)
        self.contexts.append(context)
        if self.fail:
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS,
                "crashed",
                rotate_session=True,
            )
        event = AgentEvent(
            AgentEventKind.ASSISTANT,
            "completed",
            message="done",
            provider_session_id="thread-1",
            provider_turn_id="turn-1",
        )
        result = emit(event)
        if result is not None:
            await result
        return AgentTerminalResult(
            output="done",
            events=(event,),
            provider_session_id="thread-1",
            provider_turn_id="turn-1",
            usage={"input_tokens": 2, "output_tokens": 1},
            model=self.model,
            effort=self.effort,
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_a_command")
async def test_native_chat_context_is_full_then_incremental_without_duplicates(
    monkeypatch,
    tmp_path,
    doing,
) -> None:
    use_cli_agent_slots(
        monkeypatch, "aiko", {"default": cli_agents.ExecutableInfo(adapter="codex")}
    )
    adapter = _Adapter()

    def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    doing("chat", "slack:bot:C1:100.1")
    snapshot = [
        {
            "timestamp": position(99.1),
            "author": "user",
            "author_type": "user",
            "content": "older-message",
        },
        {
            "timestamp": position(100.1),
            "author": "user",
            "author_type": "user",
            "content": "first-message",
        },
        {
            "timestamp": position(101.1),
            "author": "user",
            "author_type": "user",
            "content": "second-message",
        },
    ]
    state = {
        "agent_execution_context": {
            "resume_policy": "auto",
            "context_cursor": position(100.1),
            "rebuild_context": json.dumps(snapshot),
            "rebuild_context_complete": True,
            "continuation_input": "continue-only",
            "attempt": 1,
        }
    }
    await brain.run_with_execution_details(
        "first-turn", cwd=tmp_path, session_state=state
    )
    state["agent_execution_context"]["context_cursor"] = position(101.1)
    await brain.run_with_execution_details(
        "second-turn", cwd=tmp_path, session_state=state
    )
    state["agent_execution_context"]["attempt"] = 2
    await brain.run_with_execution_details(
        "duplicate-second-turn", cwd=tmp_path, session_state=state
    )

    assert 'mode="full"' in adapter.prompts[0]
    assert "older-message" in adapter.prompts[0]
    assert "first-message" not in adapter.prompts[0]
    assert "second-message" not in adapter.prompts[0]
    assert 'mode="incremental"' in adapter.prompts[1]
    assert "first-message" not in adapter.prompts[1]
    assert "older-message" not in adapter.prompts[1]
    assert "second-message" not in adapter.prompts[1]
    assert 'mode="continuation"' in adapter.prompts[2]
    assert "continue-only" in adapter.prompts[2]
    assert "duplicate-second-turn" not in adapter.prompts[2]


@pytest.mark.asyncio
async def test_native_chat_requires_live_inspection_when_snapshot_is_incomplete(
    monkeypatch,
    tmp_path,
    in_a_command,
    doing,
) -> None:
    use_cli_agent_slots(
        monkeypatch, "aiko", {"default": cli_agents.ExecutableInfo(adapter="codex")}
    )
    adapter = _Adapter()

    def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    doing("chat", "slack:bot:C1:100.1")
    await brain.run_with_execution_details(
        "first-turn",
        cwd=tmp_path,
        session_state={
            "agent_execution_context": {
                "resume_policy": "auto",
                "context_cursor": position(100.1),
                "rebuild_context": "[]",
                "rebuild_context_complete": False,
            }
        },
    )

    assert 'mode="inspect_required"' in adapter.prompts[0]
    assert any(
        event.kind is AgentEventKind.TURN and event.name == "started"
        for event in in_a_command.events()
    )

    async def interrupt(self):
        return None

    async def close(self):
        return None


class _CancelledAdapter(_Adapter):
    async def run_turn(self, prompt, context, conversation, emit):
        raise asyncio.CancelledError


class _AuthenticationAdapter(_Adapter):
    async def run_turn(self, prompt, context, conversation, emit):
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.AUTHENTICATION,
            "login required",
        )


class _RateLimitedOnceAdapter(_Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def run_turn(self, prompt, context, conversation, emit):
        self.calls += 1
        if self.calls == 1:
            self.prompts.append(prompt)
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.RATE_LIMITED,
                "wait",
                details={"retry_after_seconds": 1},
            )
        return await super().run_turn(prompt, context, conversation, emit)


class _CompactingAdapter(_Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.provider_sessions: list[str] = []
        self.turn = 0

    async def run_turn(self, prompt, context, conversation, emit):
        self.prompts.append(prompt)
        self.provider_sessions.append(conversation.provider_session_id)
        self.turn += 1
        events = [
            AgentEvent(
                AgentEventKind.ASSISTANT,
                "completed",
                message="done",
                provider_session_id=f"thread-{self.turn}",
            )
        ]
        if self.turn == 1:
            events.append(
                AgentEvent(
                    AgentEventKind.TURN,
                    "context_compaction",
                    provider_session_id="thread-1",
                )
            )
        for event in events:
            result = emit(event)
            if result is not None:
                await result
        return AgentTerminalResult(
            output="done",
            events=tuple(events),
            provider_session_id=f"thread-{self.turn}",
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_a_command")
async def test_native_brain_persists_cursor_only_after_terminal_success(
    monkeypatch,
    tmp_path,
    doing,
) -> None:
    use_cli_agent_slots(
        monkeypatch, "aiko", {"default": cli_agents.ExecutableInfo(adapter="codex")}
    )
    adapter = _Adapter()

    def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    doing("ticket", "issue-300")
    state = {
        "agent_execution_context": {
            "resume_policy": "auto",
            "context_cursor": "cursor-1",
        }
    }
    result = await brain.run_with_execution_details(
        "first", cwd=tmp_path, session_state=state
    )
    adapter.fail = True
    state["agent_execution_context"]["context_cursor"] = "cursor-2"
    failed = await brain.run_with_execution_details(
        "second", cwd=tmp_path, session_state=state
    )

    key = ConversationKey("aiko", "codex", "ticket", "issue-300")
    persisted = ConversationStore(tmp_path).load(key)
    assert result.provider_session_id == "thread-1"
    assert failed.error_category == "process"
    assert persisted is not None
    assert persisted.context_cursor == "cursor-1"
    assert persisted.healthy is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_a_command")
async def test_native_brain_remembers_the_sessions_effective_settings(
    monkeypatch,
    tmp_path,
    doing,
) -> None:
    """A turn that reports no values retains the last known values in the record."""
    use_cli_agent_slots(
        monkeypatch, "aiko", {"default": cli_agents.ExecutableInfo(adapter="codex")}
    )
    adapter = _Adapter()
    adapter.model = "gpt-established"
    adapter.effort = "high"

    def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    doing("ticket", "issue-363")
    state = {
        "agent_execution_context": {
            "resume_policy": "auto",
        }
    }
    first = await brain.run_with_execution_details(
        "first", cwd=tmp_path, session_state=state
    )
    adapter.model = ""
    adapter.effort = ""
    second = await brain.run_with_execution_details(
        "second", cwd=tmp_path, session_state=state
    )

    key = ConversationKey("aiko", "codex", "ticket", "issue-363")
    persisted = ConversationStore(tmp_path).load(key)
    assert (first.model, first.effort) == ("gpt-established", "high")
    # The second turn itself reported nothing ...
    assert (second.model, second.effort) == ("", "")
    # ... but the conversation retains the last values it observed.
    assert persisted is not None
    assert persisted.effective_model == "gpt-established"
    assert persisted.effective_effort == "high"


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_a_command")
async def test_native_chat_retries_event_not_sent_by_rate_limit_preflight(
    monkeypatch,
    tmp_path,
    doing,
) -> None:
    use_cli_agent_slots(
        monkeypatch, "aiko", {"default": cli_agents.ExecutableInfo(adapter="codex")}
    )
    adapter = _RateLimitedOnceAdapter()

    def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    key = ConversationKey("aiko", "codex", "chat", "slack:bot:C1:100.1")
    record = ConversationStore(tmp_path).resolve(key, ResumePolicy.AUTO)
    record.provider_session_id = "thread-1"
    record.context_cursor = position(99.1)
    ConversationStore(tmp_path).save(record)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    doing("chat", "slack:bot:C1:100.1")
    execution_context = {
        "resume_policy": "auto",
        "context_cursor": position(100.1),
        "continuation_input": "continue-only",
        "attempt": 1,
    }
    limited = await brain.run_with_execution_details(
        "new-event",
        cwd=tmp_path,
        session_state={"agent_execution_context": execution_context},
    )
    execution_context["attempt"] = 2
    completed = await brain.run_with_execution_details(
        "new-event",
        cwd=tmp_path,
        session_state={"agent_execution_context": execution_context},
    )

    assert limited.error_category == "rate_limited"
    assert completed.returncode == 0
    assert len(adapter.prompts) == 2
    assert all('mode="incremental"' in prompt for prompt in adapter.prompts)
    assert all("new-event" in prompt for prompt in adapter.prompts)
    assert "continue-only" not in adapter.prompts[1]
    persisted = ConversationStore(tmp_path).load(key)
    assert persisted is not None
    assert persisted.context_cursor == position(100.1)


class _CrashedAdapter(_Adapter):
    async def run_turn(self, prompt, context, conversation, emit):
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory.PROCESS,
            "Codex stdout closed unexpectedly.",
            details={"returncode": 1, "stderr": "error: no such option: --foo"},
            rotate_session=True,
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_a_command")
async def test_native_brain_failure_carries_what_the_tool_said_last(
    monkeypatch,
    tmp_path,
    doing,
) -> None:
    use_cli_agent_slots(
        monkeypatch, "aiko", {"default": cli_agents.ExecutableInfo(adapter="codex")}
    )

    def get_adapter(*_args):
        return _CrashedAdapter()

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    doing("ticket", "issue-300")
    result = await brain.run_with_execution_details(
        "hello",
        cwd=tmp_path,
        session_state={
            "agent_execution_context": {
                "resume_policy": "auto",
                "context_cursor": "cursor-1",
            }
        },
    )

    # A process that just ended leaves only its stderr as a lead, so the
    # reason a reader sees carries it rather than the bare "closed" claim.
    assert result.error_category == "process"
    assert result.stderr == (
        "Codex stdout closed unexpectedly.\nerror: no such option: --foo"
    )
    assert result.error_details["stderr"] == "error: no such option: --foo"


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_a_command")
async def test_native_brain_rotates_after_cancelled_turn(
    monkeypatch, tmp_path, doing
) -> None:
    use_cli_agent_slots(
        monkeypatch, "aiko", {"default": cli_agents.ExecutableInfo(adapter="codex")}
    )

    def get_adapter(*_args):
        return _CancelledAdapter()

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    doing("ticket", "issue-300")
    with pytest.raises(asyncio.CancelledError):
        await brain.run_with_execution_details(
            "cancel",
            cwd=tmp_path,
            session_state={
                "agent_execution_context": {
                    "resume_policy": "auto",
                    "context_cursor": "cursor-1",
                }
            },
        )

    key = ConversationKey("aiko", "codex", "ticket", "issue-300")
    persisted = ConversationStore(tmp_path).load(key)
    assert persisted is not None
    assert persisted.healthy is False
    assert persisted.rotation_reason == "cancelled"


@pytest.mark.asyncio
async def test_native_authentication_notification_identifies_member_and_cli(
    monkeypatch,
    tmp_path,
    in_a_command,
    doing,
) -> None:
    use_cli_agent_slots(
        monkeypatch, "aiko", {"default": cli_agents.ExecutableInfo(adapter="codex")}
    )

    def get_adapter(*_args):
        return _AuthenticationAdapter()

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    doing("ticket", "issue-300")
    with pytest.raises(agent_models.CliAgentExecutionError) as excinfo:
        await brain.run(
            "hello",
            cwd=tmp_path,
            session_state={
                "agent_execution_context": {
                    "resume_policy": "fresh",
                }
            },
        )

    # The host records it for the member the command runs as.
    assert excinfo.value.cli_agent == "codex"
    assert [(entry.tool, entry.failed) for entry in in_a_command.credentials()] == [
        ("codex", True)
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_a_command")
async def test_native_brain_rebuilds_chat_after_context_compaction(
    monkeypatch,
    tmp_path,
    doing,
) -> None:
    use_cli_agent_slots(
        monkeypatch, "aiko", {"default": cli_agents.ExecutableInfo(adapter="codex")}
    )
    adapter = _CompactingAdapter()

    def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    doing("chat", "slack:bot:C1:100.1")
    state = {
        "agent_execution_context": {
            "resume_policy": "auto",
            "context_cursor": position(100.1),
            "rebuild_context": json.dumps(
                [
                    {
                        "timestamp": position(99.1),
                        "author": "user",
                        "author_type": "user",
                        "content": "rebuild-me",
                    }
                ]
            ),
            "rebuild_context_complete": True,
        }
    }
    await brain.run_with_execution_details(
        "first-turn", cwd=tmp_path, session_state=state
    )
    compacted = ConversationStore(tmp_path).load(
        ConversationKey("aiko", "codex", "chat", "slack:bot:C1:100.1")
    )
    await brain.run_with_execution_details(
        "second-turn", cwd=tmp_path, session_state=state
    )

    assert compacted is not None
    assert compacted.healthy is False
    assert compacted.rotation_reason == "context_compaction"
    assert adapter.provider_sessions == ["", ""]
    assert all('mode="full"' in prompt for prompt in adapter.prompts)
    assert "rebuild-me" in adapter.prompts[1]


def test_agent_diagnostics_redact_credentials_and_keep_correlation(
    monkeypatch, tmp_path
) -> None:
    recorded = []
    monkeypatch.setattr(
        diagnostics,
        "record_correlated_event",
        lambda **kwargs: recorded.append(kwargs),
    )
    key = ConversationKey("aiko", "codex", "ticket", "issue-300")
    from guildbotics.intelligences.agent_runtime.models import (
        AgentExecutionContext,
        ConversationRecord,
    )

    context = AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=tmp_path,
        conversation_key=key,
        context_cursor="cursor-1",
    )
    diagnostics.record_agent_event(
        AgentEvent(
            AgentEventKind.FAILED,
            "failed",
            message="Authorization: Bearer top-secret",
            command="tool --token command-secret",
            details={
                "access_token": "secret",
                "nested": {"password": "secret"},
                "output": "API_KEY=output-secret",
            },
        ),
        context,
        ConversationRecord(key=key, generation=2),
    )

    assert recorded[0]["event_type"] == "agent_runtime.failed"
    assert recorded[0]["attributes"]["agent.run_id"] == "run-1"
    assert recorded[0]["attributes"]["agent.conversation_generation"] == 2
    assert recorded[0]["payload"]["details"] == {
        "access_token": "***",
        "nested": {"password": "***"},
        "output": "API_KEY=***",
    }
    assert recorded[0]["payload"]["message"] == "Authorization: ***"
    assert recorded[0]["payload"]["command"] == "tool --token ***"


def test_agent_diagnostics_skips_assistant_deltas(monkeypatch, tmp_path) -> None:
    recorded = []
    monkeypatch.setattr(
        diagnostics,
        "record_correlated_event",
        lambda **kwargs: recorded.append(kwargs),
    )
    key = ConversationKey("aiko", "codex", "ticket", "issue-300")
    from guildbotics.intelligences.agent_runtime.models import (
        AgentExecutionContext,
        ConversationRecord,
    )

    context = AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=tmp_path,
        conversation_key=key,
    )
    conversation = ConversationRecord(key=key)

    diagnostics.record_agent_event(
        AgentEvent(AgentEventKind.ASSISTANT, "delta", message="partial"),
        context,
        conversation,
    )
    diagnostics.record_agent_event(
        AgentEvent(AgentEventKind.ASSISTANT, "completed", message="complete"),
        context,
        conversation,
    )

    assert len(recorded) == 1
    assert recorded[0]["payload"]["message"] == "complete"


@pytest.fixture
def native_aiko(monkeypatch):
    use_cli_agent_slots(
        monkeypatch, "aiko", {"default": cli_agents.ExecutableInfo(adapter="codex")}
    )


_CHAT_IDENTITY = "slack:bot:C1:100.1"


def _seed_chat_record(
    tmp_path, *, cursor: str, last_run_id: str = "", last_event_id: str = ""
) -> ConversationKey:
    key = ConversationKey("aiko", "codex", "chat", _CHAT_IDENTITY)
    store = ConversationStore(tmp_path)
    record = store.resolve(key, ResumePolicy.AUTO)
    record.provider_session_id = "thread-0"
    record.context_cursor = cursor
    record.last_run_id = last_run_id
    record.last_event_id = last_event_id
    store.save(record)
    return key


async def _run_chat_turn(
    brain,
    tmp_path,
    doing,
    *,
    cursor: str,
    run_id: str = "run-A",
    event_id: str = "EA",
):
    doing("chat", _CHAT_IDENTITY, run_id)
    return await brain.run_with_execution_details(
        "retry-turn",
        cwd=tmp_path,
        session_state={
            "agent_execution_context": {
                "resume_policy": "auto",
                "context_cursor": cursor,
                "event_id": event_id,
                "rebuild_context": "[]",
                "rebuild_context_complete": True,
                "continuation_input": "continue-only",
                "attempt": 1,
            }
        },
    )


@pytest.mark.asyncio
async def test_native_chat_cursor_regression_rotates_instead_of_continuing(
    monkeypatch,
    tmp_path,
    native_aiko,
    in_a_command,
    doing,
) -> None:
    adapter = _Adapter()

    def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    key = _seed_chat_record(
        tmp_path, cursor=position(200.1), last_run_id="run-B", last_event_id="EB"
    )
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())

    result = await _run_chat_turn(brain, tmp_path, doing, cursor=position(100.1))

    # The overtaken event is re-fed with full context on a fresh session; the
    # generic continuation prompt (which would let the agent mistake run-B's
    # completion for run-A's) is never used.
    assert result.returncode == 0
    assert 'mode="full"' in adapter.prompts[0]
    assert "continue-only" not in adapter.prompts[0]
    rejections = [
        event
        for event in in_a_command.events()
        if event.kind is AgentEventKind.TURN and event.name == "continuation_rejected"
    ]
    assert len(rejections) == 1
    assert rejections[0].details["reason"] == "cursor_regression"
    assert rejections[0].details["current_cursor"] == position(100.1)
    assert rejections[0].details["persisted_cursor"] == position(200.1)
    assert rejections[0].details["run_id"] == "run-A"
    assert rejections[0].details["last_run_id"] == "run-B"
    persisted = ConversationStore(tmp_path).load(key)
    assert persisted is not None
    assert persisted.generation == 1
    assert persisted.rotation_reason == "cursor_regression"
    assert persisted.context_cursor == position(100.1)
    assert persisted.last_run_id == "run-A"
    assert persisted.last_event_id == "EA"


@pytest.mark.asyncio
async def test_native_chat_same_cursor_same_run_event_uses_continuation(
    monkeypatch,
    tmp_path,
    native_aiko,
    in_a_command,
    doing,
) -> None:
    adapter = _Adapter()

    def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    _seed_chat_record(
        tmp_path, cursor=position(100.1), last_run_id="run-A", last_event_id="EA"
    )
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())

    result = await _run_chat_turn(brain, tmp_path, doing, cursor=position(100.1))

    assert result.returncode == 0
    assert 'mode="continuation"' in adapter.prompts[0]
    assert "continue-only" in adapter.prompts[0]
    assert not any(
        event.name == "continuation_rejected" for event in in_a_command.events()
    )


@pytest.mark.asyncio
async def test_native_chat_same_cursor_different_run_is_not_continuation(
    monkeypatch,
    tmp_path,
    native_aiko,
    in_a_command,
    doing,
) -> None:
    adapter = _Adapter()

    def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    _seed_chat_record(
        tmp_path, cursor=position(100.1), last_run_id="run-B", last_event_id="EB"
    )
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())

    result = await _run_chat_turn(brain, tmp_path, doing, cursor=position(100.1))

    assert result.returncode == 0
    assert 'mode="full"' in adapter.prompts[0]
    assert "continue-only" not in adapter.prompts[0]
    rejections = [e for e in in_a_command.events() if e.name == "continuation_rejected"]
    assert len(rejections) == 1
    assert rejections[0].details["reason"] == "identity_mismatch"


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_a_command")
async def test_native_chat_legacy_record_without_identity_rotates_to_full_context(
    monkeypatch,
    tmp_path,
    native_aiko,
    doing,
) -> None:
    adapter = _Adapter()

    def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    key = _seed_chat_record(tmp_path, cursor=position(100.1))
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())

    result = await _run_chat_turn(brain, tmp_path, doing, cursor=position(100.1))

    # A record predating run/event identity cannot prove the session targeted
    # this run, so it is rotated instead of continued.
    assert result.returncode == 0
    assert 'mode="full"' in adapter.prompts[0]
    assert "continue-only" not in adapter.prompts[0]
    persisted = ConversationStore(tmp_path).load(key)
    assert persisted is not None
    assert persisted.generation == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_a_command")
async def test_resumed_chat_receives_whole_unread_batch_and_intervening_context(
    monkeypatch,
    tmp_path,
    doing,
):
    use_cli_agent_slots(
        monkeypatch,
        "aiko",
        {
            "default": cli_agents.ExecutableInfo(adapter="codex"),
        },
    )
    adapter = _Adapter()

    def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(cli_agent, "create_native_adapter", get_adapter)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    doing("chat", "slack:bot:C1:100.1", "batch-1")
    configured = {
        "resume_policy": "auto",
        "context_cursor": position(100.1),
        "event_id": "E1",
        "rebuild_context": "[]",
        "rebuild_context_complete": True,
    }
    await brain.run_with_execution_details(
        "first request",
        cwd=tmp_path,
        session_state={"agent_execution_context": configured},
    )
    doing("chat", "slack:bot:C1:100.1", "batch-2")
    configured.update(
        {
            "context_cursor": position(105.1),
            "event_id": "E3",
            "rebuild_context": json.dumps(
                [
                    {"timestamp": position(100.1), "content": "already delivered"},
                    {
                        "timestamp": position(102.1),
                        "content": "discussion between B and C",
                    },
                    {"timestamp": position(104.1), "content": "additional context"},
                    {"timestamp": position(107.1), "content": "future request"},
                ]
            ),
        }
    )
    unread = '<unprocessed_messages>["request at 3", "correction at 5"]</unprocessed_messages>'
    await brain.run_with_execution_details(
        unread,
        cwd=tmp_path,
        session_state={"agent_execution_context": configured},
    )
    prompt = adapter.prompts[-1]
    assert 'mode="incremental"' in prompt
    assert unread in prompt
    assert "discussion between B and C" in prompt
    assert "additional context" in prompt
    assert "already delivered" not in prompt
    assert "future request" not in prompt


@pytest.mark.asyncio
async def test_each_turn_speaks_through_an_adapter_of_its_own(
    monkeypatch, tmp_path, in_a_command, native_aiko
) -> None:
    """A provider starts with its turn and ends with it; the adapter it is
    spoken to through is the turn's, closed when the turn ends however it
    ends."""
    adapters: list[_Adapter] = []

    def create_adapter(_name):
        adapters.append(_CrashedAdapter() if adapters else _Adapter())
        return adapters[-1]

    monkeypatch.setattr(cli_agent, "create_native_adapter", create_adapter)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())

    for _ in range(2):
        await brain.run_with_execution_details("go", cwd=tmp_path)

    assert len(adapters) == 2
    assert all(adapter.closed for adapter in adapters)


@pytest.mark.asyncio
async def test_a_turn_runs_only_inside_a_command(tmp_path, native_aiko) -> None:
    """Every turn runs in the environment of the command it belongs to; with
    no window to the host there is no command to run it in."""
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())

    with pytest.raises(AgentRuntimeError) as refused:
        await brain.run_with_execution_details("go", cwd=tmp_path)

    assert refused.value.category is AgentRuntimeErrorCategory.CONFIGURATION


@pytest.mark.asyncio
async def test_a_turn_works_for_the_commands_run_and_work_whatever_it_is_told(
    monkeypatch, tmp_path, in_a_command, native_aiko
) -> None:
    """The host holds every turn of a command to the command's run and work:
    what the command's code passes cannot name another run or another work's
    conversation, and the turn's answer points at the command's trace."""
    adapter = _Adapter()
    monkeypatch.setattr(cli_agent, "create_native_adapter", lambda _name: adapter)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())

    await brain.run_with_execution_details(
        "go",
        cwd=tmp_path,
        session_state={
            "agent_execution_context": {
                "run_id": "run-other",
                "work_kind": "ticket",
                "work_identity": "https://example.test/other",
            }
        },
    )

    (context,) = adapter.contexts
    assert context.run_id == "command-run"
    assert context.trace_id == "command-trace"
    assert context.conversation_key.work_kind == "manual"
    assert context.conversation_key.work_identity == "command-run"
