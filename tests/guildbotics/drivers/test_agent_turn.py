from __future__ import annotations

from typing import Any

import pytest

from guildbotics.capabilities.completion_retry import CompletionRetryExhausted
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.commands.errors import CommandError
from guildbotics.drivers import agent_turn
from guildbotics.drivers.agent_turn import run_agent_turn
from guildbotics.intelligences.brains.cli_agent import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)


def _store(tmp_path) -> RunStore:
    return RunStore(tmp_path / ".guildbotics" / "state" / "task-runs")


def _ticket_completion(store: RunStore, run_id: str) -> None:
    store.append_evidence(run_id, "issue_comment", {"url": "https://example.test/1"})
    store.complete(
        run_id,
        "done",
        "completed",
        "https://example.test/1",
        "aiko",
    )


@pytest.mark.asyncio
async def test_host_retries_with_continuation_and_returns_completion(tmp_path):
    contexts: list[dict[str, Any]] = []
    store = _store(tmp_path)

    async def invoke(context: dict[str, Any]) -> str:
        contexts.append(context)
        if len(contexts) == 2:
            _ticket_completion(store, "run-1")
        return f"response-{len(contexts)}"

    result = await run_agent_turn(
        invoke=invoke,
        execution_context={
            "run_id": "run-1",
            "workspace_data_root": str(tmp_path),
            "work_kind": "ticket",
            "work_identity": "https://example.test/1",
            "resume_policy": "fresh",
            "attempt": 1,
            "max_completion_attempts": 3,
        },
    )

    assert result.response == "response-2"
    assert result.completion.status == "done"
    assert [item["evidence_type"] for item in result.evidence] == ["issue_comment"]
    assert [context["attempt"] for context in contexts] == [1, 2]
    assert [context["resume_policy"] for context in contexts] == ["fresh", "auto"]
    assert "run-1" in contexts[1]["continuation_input"]


@pytest.mark.asyncio
async def test_host_returns_chat_evidence_from_the_same_completion_boundary(tmp_path):
    store = _store(tmp_path)
    contexts: list[dict[str, Any]] = []

    async def invoke(context: dict[str, Any]) -> str:
        contexts.append(context)
        store.append_evidence("run-chat", "chat_reply", {"message_ts": "1.2"})
        store.complete_run(
            "run-chat",
            "done",
            "replied",
            subject_type="chat",
            subject_id="E1",
            person_id="aiko",
        )
        return "response"

    result = await run_agent_turn(
        invoke=invoke,
        execution_context={
            "run_id": "run-chat",
            "workspace_data_root": str(tmp_path),
            "work_kind": "chat",
            "work_identity": "slack:C1:T1",
            "event_id": "E1",
            "max_completion_attempts": 2,
        },
    )

    assert result.completion.subject_type == "chat"
    assert result.evidence[0]["evidence_type"] == "chat_reply"
    assert "run-chat" in contexts[0]["continuation_input"]
    assert "E1" in contexts[0]["continuation_input"]


@pytest.mark.asyncio
async def test_host_exhausts_the_configured_attempt_budget(tmp_path):
    attempts: list[int] = []

    async def invoke(context: dict[str, Any]) -> str:
        attempts.append(context["attempt"])
        return "response"

    with pytest.raises(CompletionRetryExhausted) as excinfo:
        await run_agent_turn(
            invoke=invoke,
            execution_context={
                "run_id": "run-1",
                "workspace_data_root": str(tmp_path),
                "work_kind": "ticket",
                "max_completion_attempts": 2,
            },
        )

    assert attempts == [1, 2]
    assert excinfo.value.attempts == 2


@pytest.mark.parametrize("failure_stage", ["invoke", "completion"])
@pytest.mark.asyncio
async def test_host_reraises_rate_limits_without_retrying(
    tmp_path, monkeypatch, failure_stage
):
    calls = 0
    rate_limited = CliAgentExecutionError(
        cli_agent="codex",
        result=CliAgentExecutionResult(
            stdout="",
            stderr="rate limit",
            returncode=75,
            error_category="rate_limited",
            error_details={"retry_after_text": "11:44 AM"},
        ),
    )

    def raise_wrapped_rate_limit(*args, **kwargs):
        raise CommandError("wrapped") from rate_limited

    async def invoke(context: dict[str, Any]) -> str:
        nonlocal calls
        calls += 1
        if failure_stage == "invoke":
            raise_wrapped_rate_limit()
        return "response"

    if failure_stage == "completion":
        monkeypatch.setattr(agent_turn.RunStore, "status", raise_wrapped_rate_limit)

    with pytest.raises(CliAgentExecutionError) as excinfo:
        await run_agent_turn(
            invoke=invoke,
            execution_context={
                "run_id": "run-1",
                "workspace_data_root": str(tmp_path),
                "work_kind": "ticket",
                "max_completion_attempts": 3,
            },
        )

    assert calls == 1
    assert excinfo.value is rate_limited


@pytest.mark.asyncio
async def test_host_records_completion_missing_then_completed_events(
    tmp_path, monkeypatch
):
    completion_call = 2
    max_attempts = 3
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        agent_turn,
        "record_workflow_completed",
        lambda **kwargs: recorded.append(("completed", kwargs)),
    )
    monkeypatch.setattr(
        agent_turn,
        "record_workflow_completion_missing",
        lambda **kwargs: recorded.append(("missing", kwargs)),
    )
    store = _store(tmp_path)
    calls = 0

    async def invoke(context: dict[str, Any]) -> str:
        nonlocal calls
        calls += 1
        if calls == completion_call:
            _ticket_completion(store, "run-1")
        return "response"

    await run_agent_turn(
        invoke=invoke,
        execution_context={
            "run_id": "run-1",
            "workspace_data_root": str(tmp_path),
            "work_kind": "ticket",
            "attempt": 4,
            "max_completion_attempts": max_attempts,
        },
    )

    assert [name for name, _ in recorded] == ["missing", "completed"]
    missing = recorded[0][1]
    assert missing["run_id"] == "run-1"
    assert missing["attempt"] == 1
    assert missing["max_attempts"] == max_attempts
    assert "not found" in missing["error"].lower()
    assert recorded[1][1] == {"run_id": "run-1", "attempt": 2}


@pytest.mark.asyncio
async def test_host_does_not_record_completion_missing_for_invoke_failures(
    tmp_path, monkeypatch
):
    recorded: list[str] = []
    monkeypatch.setattr(
        agent_turn,
        "record_workflow_completion_missing",
        lambda **kwargs: recorded.append("missing"),
    )

    async def invoke(context: dict[str, Any]) -> str:
        raise RuntimeError("provider turn failed")

    with pytest.raises(CompletionRetryExhausted):
        await run_agent_turn(
            invoke=invoke,
            execution_context={
                "run_id": "run-1",
                "workspace_data_root": str(tmp_path),
                "work_kind": "ticket",
                "max_completion_attempts": 2,
            },
        )

    assert recorded == []


@pytest.mark.asyncio
async def test_host_can_leave_provider_failures_to_the_chat_dispatcher(tmp_path):
    calls = 0

    async def invoke(context: dict[str, Any]) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        await run_agent_turn(
            invoke=invoke,
            execution_context={
                "run_id": "run-chat",
                "workspace_data_root": str(tmp_path),
                "work_kind": "chat",
                "max_completion_attempts": 3,
                "retry_invoke_exceptions": False,
            },
        )

    assert calls == 1
