from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from guildbotics.intelligences.agent_runtime import diagnostics, registry
from guildbotics.intelligences.agent_runtime.environment import (
    isolated_agent_environment,
    member_command_environment,
    remove_isolated_config,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentEventKind,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    AgentTerminalResult,
    ConversationKey,
    ResumePolicy,
)
from guildbotics.intelligences.agent_runtime.store import ConversationStore
from guildbotics.intelligences.brains import cli_agent


class _Logger:
    debug = info = warning = error = lambda *args, **kwargs: None


class _Adapter:
    name = "codex-app-server"

    def __init__(self) -> None:
        self.fail = False
        self.prompts: list[str] = []
        self.contexts = []

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
        )


@pytest.mark.asyncio
async def test_native_chat_context_is_full_then_incremental_without_duplicates(
    monkeypatch, tmp_path
) -> None:
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["aiko"] = {
        "default": cli_agent.ExecutableInfo(adapter="codex")
    }
    adapter = _Adapter()

    async def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(registry, "get_native_adapter", get_adapter)
    monkeypatch.setattr(diagnostics, "record_agent_event", lambda *args: None)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    snapshot = [
        {
            "timestamp": "99.1",
            "author": "user",
            "author_type": "user",
            "content": "older-message",
        },
        {
            "timestamp": "100.1",
            "author": "user",
            "author_type": "user",
            "content": "first-message",
        },
        {
            "timestamp": "101.1",
            "author": "user",
            "author_type": "user",
            "content": "second-message",
        },
    ]
    state = {
        "agent_execution_context": {
            "run_id": "run-1",
            "workspace_data_root": str(tmp_path),
            "work_kind": "chat",
            "work_identity": "slack:bot:C1:100.1",
            "resume_policy": "auto",
            "context_cursor": "100.1",
            "rebuild_context": json.dumps(snapshot),
            "rebuild_context_complete": True,
            "continuation_input": "continue-only",
            "attempt": 1,
        }
    }
    try:
        await brain.run_with_execution_details(
            "first-turn", cwd=tmp_path, session_state=state
        )
        state["agent_execution_context"]["context_cursor"] = "101.1"
        await brain.run_with_execution_details(
            "second-turn", cwd=tmp_path, session_state=state
        )
        state["agent_execution_context"]["attempt"] = 2
        await brain.run_with_execution_details(
            "duplicate-second-turn", cwd=tmp_path, session_state=state
        )
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

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
    assert all(context.lease_id for context in adapter.contexts)


@pytest.mark.asyncio
async def test_native_brain_releases_run_binding_between_calls(
    monkeypatch, tmp_path
) -> None:
    from guildbotics.runtime.person_lease import PersonExecutionLease

    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["aiko"] = {
        "default": cli_agent.ExecutableInfo(adapter="codex")
    }
    adapter = _Adapter()

    async def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(registry, "get_native_adapter", get_adapter)
    monkeypatch.setattr(diagnostics, "record_agent_event", lambda *args: None)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    lease = PersonExecutionLease("aiko", tmp_path)
    lease.acquire(source="routine", command="ticket", work_id="work-1")
    try:
        for run_id in ("run-1", "run-2"):
            await brain.run_with_execution_details(
                "turn",
                cwd=tmp_path,
                session_state={
                    "agent_execution_context": {
                        "run_id": run_id,
                        "workspace_data_root": str(tmp_path),
                        "work_kind": "ticket",
                        "work_identity": "issue-300",
                        "resume_policy": "fresh",
                    }
                },
            )
            assert lease.metadata.run_id == ""
    finally:
        lease.release()
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert [context.run_id for context in adapter.contexts] == ["run-1", "run-2"]


@pytest.mark.asyncio
async def test_native_chat_requires_live_inspection_when_snapshot_is_incomplete(
    monkeypatch, tmp_path
) -> None:
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["aiko"] = {
        "default": cli_agent.ExecutableInfo(adapter="codex")
    }
    adapter = _Adapter()

    async def get_adapter(*_args):
        return adapter

    recorded_events: list[AgentEvent] = []
    monkeypatch.setattr(registry, "get_native_adapter", get_adapter)
    monkeypatch.setattr(
        diagnostics,
        "record_agent_event",
        lambda event, *_args: recorded_events.append(event),
    )
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    try:
        await brain.run_with_execution_details(
            "first-turn",
            cwd=tmp_path,
            session_state={
                "agent_execution_context": {
                    "run_id": "run-1",
                    "workspace_data_root": str(tmp_path),
                    "work_kind": "chat",
                    "work_identity": "slack:bot:C1:100.1",
                    "resume_policy": "auto",
                    "context_cursor": "100.1",
                    "rebuild_context": "[]",
                    "rebuild_context_complete": False,
                }
            },
        )
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert 'mode="inspect_required"' in adapter.prompts[0]
    assert any(
        event.kind is AgentEventKind.TURN and event.name == "started"
        for event in recorded_events
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


class _TrackedAdapter(_Adapter):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_native_brain_persists_cursor_only_after_terminal_success(
    monkeypatch, tmp_path
) -> None:
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["aiko"] = {
        "default": cli_agent.ExecutableInfo(adapter="codex")
    }
    adapter = _Adapter()

    async def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(registry, "get_native_adapter", get_adapter)
    monkeypatch.setattr(diagnostics, "record_agent_event", lambda *args: None)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    state = {
        "agent_execution_context": {
            "run_id": "run-1",
            "workspace_data_root": str(tmp_path),
            "work_kind": "ticket",
            "work_identity": "issue-300",
            "resume_policy": "auto",
            "context_cursor": "cursor-1",
        }
    }
    try:
        result = await brain.run_with_execution_details(
            "first", cwd=tmp_path, session_state=state
        )
        adapter.fail = True
        state["agent_execution_context"]["context_cursor"] = "cursor-2"
        failed = await brain.run_with_execution_details(
            "second", cwd=tmp_path, session_state=state
        )
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    key = ConversationKey("aiko", "codex", "ticket", "issue-300")
    persisted = ConversationStore(tmp_path).load(key)
    assert result.provider_session_id == "thread-1"
    assert failed.error_category == "process"
    assert persisted is not None
    assert persisted.context_cursor == "cursor-1"
    assert persisted.healthy is False


@pytest.mark.asyncio
async def test_native_chat_retries_event_not_sent_by_rate_limit_preflight(
    monkeypatch, tmp_path
) -> None:
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["aiko"] = {
        "default": cli_agent.ExecutableInfo(adapter="codex")
    }
    adapter = _RateLimitedOnceAdapter()

    async def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(registry, "get_native_adapter", get_adapter)
    monkeypatch.setattr(diagnostics, "record_agent_event", lambda *args: None)
    key = ConversationKey("aiko", "codex", "chat", "slack:bot:C1:100.1")
    record = ConversationStore(tmp_path).resolve(key, ResumePolicy.AUTO)
    record.provider_session_id = "thread-1"
    record.context_cursor = "99.1"
    ConversationStore(tmp_path).save(record)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    execution_context = {
        "run_id": "run-1",
        "workspace_data_root": str(tmp_path),
        "work_kind": "chat",
        "work_identity": "slack:bot:C1:100.1",
        "resume_policy": "auto",
        "context_cursor": "100.1",
        "continuation_input": "continue-only",
        "attempt": 1,
    }
    try:
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
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert limited.error_category == "rate_limited"
    assert completed.returncode == 0
    assert len(adapter.prompts) == 2
    assert all('mode="incremental"' in prompt for prompt in adapter.prompts)
    assert all("new-event" in prompt for prompt in adapter.prompts)
    assert "continue-only" not in adapter.prompts[1]
    persisted = ConversationStore(tmp_path).load(key)
    assert persisted is not None
    assert persisted.context_cursor == "100.1"


@pytest.mark.asyncio
async def test_native_brain_does_not_contact_provider_when_person_lease_conflicts(
    monkeypatch, tmp_path
) -> None:
    from guildbotics.runtime import person_lease

    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["aiko"] = {
        "default": cli_agent.ExecutableInfo(adapter="codex", agent_name="codex")
    }
    held = person_lease.PersonExecutionLease("aiko", tmp_path)
    held.acquire(source="routine", command="ticket", work_id="other-work")
    provider_calls = 0

    async def get_adapter(*_args):
        nonlocal provider_calls
        provider_calls += 1
        return _Adapter()

    monkeypatch.setattr(registry, "get_native_adapter", get_adapter)
    monkeypatch.setattr(person_lease, "current_person_lease", lambda: None)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    try:
        result = await brain.run_with_execution_details(
            "hello",
            cwd=tmp_path,
            session_state={
                "agent_execution_context": {
                    "run_id": "run-1",
                    "workspace_data_root": str(tmp_path),
                    "work_kind": "ticket",
                    "work_identity": "issue-300",
                    "resume_policy": "fresh",
                }
            },
        )
    finally:
        held.release()
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert result.error_category == "lease_unavailable"
    assert provider_calls == 0


@pytest.mark.asyncio
async def test_native_brain_rotates_after_cancelled_turn(monkeypatch, tmp_path) -> None:
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["aiko"] = {
        "default": cli_agent.ExecutableInfo(adapter="codex")
    }

    async def get_adapter(*_args):
        return _CancelledAdapter()

    monkeypatch.setattr(registry, "get_native_adapter", get_adapter)
    monkeypatch.setattr(diagnostics, "record_agent_event", lambda *args: None)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    try:
        with pytest.raises(asyncio.CancelledError):
            await brain.run_with_execution_details(
                "cancel",
                cwd=tmp_path,
                session_state={
                    "agent_execution_context": {
                        "run_id": "run-1",
                        "workspace_data_root": str(tmp_path),
                        "work_kind": "ticket",
                        "work_identity": "issue-300",
                        "resume_policy": "auto",
                        "context_cursor": "cursor-1",
                    }
                },
            )
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    key = ConversationKey("aiko", "codex", "ticket", "issue-300")
    persisted = ConversationStore(tmp_path).load(key)
    assert persisted is not None
    assert persisted.healthy is False
    assert persisted.rotation_reason == "cancelled"


@pytest.mark.asyncio
async def test_native_authentication_notification_identifies_member_and_cli(
    monkeypatch, tmp_path
) -> None:
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["aiko"] = {
        "default": cli_agent.ExecutableInfo(adapter="codex", agent_name="codex")
    }

    async def get_adapter(*_args):
        return _AuthenticationAdapter()

    recorded: list[dict] = []
    monkeypatch.setattr(registry, "get_native_adapter", get_adapter)
    monkeypatch.setattr(diagnostics, "record_agent_event", lambda *args: None)
    monkeypatch.setattr(
        cli_agent,
        "record_correlated_event",
        lambda **kwargs: recorded.append(kwargs),
    )
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    try:
        with pytest.raises(cli_agent.CliAgentExecutionError) as excinfo:
            await brain.run(
                "hello",
                cwd=tmp_path,
                session_state={
                    "agent_execution_context": {
                        "run_id": "run-1",
                        "workspace_data_root": str(tmp_path),
                        "work_kind": "ticket",
                        "work_identity": "issue-300",
                        "resume_policy": "fresh",
                    }
                },
            )
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert excinfo.value.cli_agent == "codex"
    assert recorded[0]["person_id"] == "aiko"
    assert recorded[0]["payload"] == {
        "provider": "cli_agent",
        "cli_agent": "codex",
        "person_id": "aiko",
        "code": "authentication",
    }


@pytest.mark.asyncio
async def test_native_brain_rebuilds_chat_after_context_compaction(
    monkeypatch, tmp_path
) -> None:
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["aiko"] = {
        "default": cli_agent.ExecutableInfo(adapter="codex")
    }
    adapter = _CompactingAdapter()

    async def get_adapter(*_args):
        return adapter

    monkeypatch.setattr(registry, "get_native_adapter", get_adapter)
    monkeypatch.setattr(diagnostics, "record_agent_event", lambda *args: None)
    brain = cli_agent.CliAgentBrain("aiko", "native", _Logger())
    state = {
        "agent_execution_context": {
            "run_id": "run-1",
            "workspace_data_root": str(tmp_path),
            "work_kind": "chat",
            "work_identity": "slack:bot:C1:100.1",
            "resume_policy": "auto",
            "context_cursor": "100.1",
            "rebuild_context": json.dumps(
                [
                    {
                        "timestamp": "99.1",
                        "author": "user",
                        "author_type": "user",
                        "content": "rebuild-me",
                    }
                ]
            ),
            "rebuild_context_complete": True,
        }
    }
    try:
        await brain.run_with_execution_details(
            "first-turn", cwd=tmp_path, session_state=state
        )
        compacted = ConversationStore(tmp_path).load(
            ConversationKey("aiko", "codex", "chat", "slack:bot:C1:100.1")
        )
        await brain.run_with_execution_details(
            "second-turn", cwd=tmp_path, session_state=state
        )
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert compacted is not None
    assert compacted.healthy is False
    assert compacted.rotation_reason == "context_compaction"
    assert adapter.provider_sessions == ["", ""]
    assert all('mode="full"' in prompt for prompt in adapter.prompts)
    assert "rebuild-me" in adapter.prompts[1]


@pytest.mark.asyncio
async def test_registry_keeps_only_one_native_process_per_person(monkeypatch) -> None:
    created: list[_TrackedAdapter] = []

    def create_adapter(name: str, _person_id: str):
        adapter = _TrackedAdapter(name)
        created.append(adapter)
        return adapter

    monkeypatch.setattr(registry, "create_native_adapter", create_adapter)

    first = await registry.get_native_adapter("aiko", "codex", "run-1")
    assert await registry.get_native_adapter("aiko", "codex", "run-1") is first
    second = await registry.get_native_adapter("aiko", "claude", "run-2")

    assert first.closed is True
    assert second.closed is False
    other_person = await registry.get_native_adapter("yuki", "codex", "run-3")
    assert second.closed is False
    assert other_person.closed is False
    await registry.close_native_adapters()
    assert second.closed is True
    assert other_person.closed is True


def test_native_environment_removes_direct_write_credentials(
    monkeypatch, tmp_path
) -> None:
    for key in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
    ):
        monkeypatch.setenv(key, "secret")

    env, gh_config_dir = isolated_agent_environment(tmp_path)
    try:
        assert all(
            key not in env
            for key in (
                "GH_TOKEN",
                "GITHUB_TOKEN",
                "GITHUB_ENTERPRISE_TOKEN",
                "GIT_ASKPASS",
                "SSH_ASKPASS",
                "SSH_AUTH_SOCK",
            )
        )
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert "IdentityFile=/dev/null" in env["GIT_SSH_COMMAND"]
        assert env["GH_CONFIG_DIR"] == gh_config_dir
    finally:
        remove_isolated_config(gh_config_dir)


def test_member_command_environment_carries_only_execution_metadata(tmp_path) -> None:
    from guildbotics.capabilities.task_runs import RUN_ENV
    from guildbotics.intelligences.agent_runtime.models import AgentExecutionContext

    context = AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=ConversationKey("aiko", "codex", "chat", "slack:bot:C1:100.1"),
        participant_labels='{"U1":"aiko"}',
    )

    assert member_command_environment(context) == {
        "GUILDBOTICS_DATA_DIR": str(tmp_path),
        RUN_ENV: "run-1",
        "GUILDBOTICS_CHAT_PARTICIPANT_LABELS": '{"U1":"aiko"}',
    }


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
        workspace_data_root=tmp_path,
        conversation_key=key,
        context_cursor="cursor-1",
        lease_id="lease-1",
    )
    diagnostics.record_agent_event(
        AgentEvent(
            AgentEventKind.ERROR,
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

    assert recorded[0]["event_type"] == "agent_runtime.error"
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
    monkeypatch.setenv("GUILDBOTICS_TRANSCRIPT_DETAIL", "standard")
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
        workspace_data_root=tmp_path,
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


@pytest.mark.asyncio
async def test_native_registry_serializes_replacement_for_same_execution(
    monkeypatch,
) -> None:
    class _RegistryAdapter:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            await asyncio.sleep(0)
            self.closed = True

        async def interrupt(self) -> None:
            return None

    created: list[_RegistryAdapter] = []

    def create_adapter(*_args):
        adapter = _RegistryAdapter()
        created.append(adapter)
        return adapter

    monkeypatch.setattr(registry, "create_native_adapter", create_adapter)
    first = await registry.get_native_adapter("aiko", "codex", "run-1")

    replacements = await asyncio.gather(
        registry.get_native_adapter("aiko", "codex", "run-2"),
        registry.get_native_adapter("aiko", "codex", "run-2"),
    )

    assert replacements[0] is replacements[1]
    assert len(created) == 2
    assert first.closed is True
    await registry.close_native_adapters()
