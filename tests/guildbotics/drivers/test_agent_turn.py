from __future__ import annotations

from typing import Any

import pytest

from guildbotics.capabilities.completion_retry import CompletionRetryExhausted
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.drivers.agent_turn import run_agent_turn


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
