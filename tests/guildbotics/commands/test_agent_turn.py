from __future__ import annotations

import json
from typing import Any

import pytest

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.commands.agent_turn import CompletionRetryExhausted, run_agent_turn
from guildbotics.commands.errors import CommandError
from guildbotics.drivers import command_runner
from guildbotics.drivers.command_runner import HostRunLedger
from guildbotics.intelligences.agent_runtime.models import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)
from guildbotics.runtime.member_invocation import Work
from guildbotics.utils.i18n_tool import t


def _store(tmp_path) -> RunStore:
    # The host ledger locates the record from the workspace, which the suite
    # points at ``tmp_path``.
    return RunStore(tmp_path / ".guildbotics" / "state" / "task-runs")


def _chat_run(store: RunStore, run_id: str) -> None:
    """Start the run the way chat selection does before it asks for a turn."""
    store.start_record(
        run_id,
        work_kind="workflows/chat_conversation_workflow",
        execution_mode="autonomous",
        member_id="aiko",
    )
    store.append_evidence(run_id, "chat_batch", {"event_ids": ["E1"]})


def _ticket_completion(store: RunStore, run_id: str, status: str = "done") -> None:
    store.append_evidence(run_id, "issue_comment", {"url": "https://example.test/1"})
    store.complete(
        run_id,
        status,
        "completed",
        Work.of_ticket("https://example.test/1"),
        "aiko",
    )


@pytest.mark.asyncio
async def test_turn_retries_with_continuation_and_returns_the_completed_response(
    tmp_path,
):
    contexts: list[dict[str, Any]] = []
    store = _store(tmp_path)

    async def invoke(context: dict[str, Any], _parameters: dict[str, str]) -> str:
        contexts.append(context)
        if len(contexts) == 2:
            _ticket_completion(store, "run-1")
        return f"response-{len(contexts)}"

    result = await run_agent_turn(
        invoke=invoke,
        ledger=HostRunLedger("run-1", Work.of_ticket("https://example.test/1")),
        execution_context={
            "resume_policy": "fresh",
            "attempt": 1,
            "max_completion_attempts": 3,
        },
    )

    assert result == "response-2"
    assert [context["attempt"] for context in contexts] == [1, 2]
    assert [context["resume_policy"] for context in contexts] == ["fresh", "auto"]
    assert "run-1" in contexts[1]["continuation_input"]


@pytest.mark.asyncio
async def test_turn_accepts_a_chat_completion_on_the_same_boundary(tmp_path):
    store = _store(tmp_path)
    _chat_run(store, "run-chat")
    contexts: list[dict[str, Any]] = []

    async def invoke(context: dict[str, Any], _parameters: dict[str, str]) -> str:
        contexts.append(context)
        store.append_evidence("run-chat", "chat_reply", {"message_id": "1.2"})
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
        ledger=HostRunLedger("run-chat", Work("chat", "slack:C1:T1")),
        execution_context={
            "event_id": "E1",
            "max_completion_attempts": 2,
        },
    )

    assert result == "response"
    assert len(contexts) == 1
    assert "run-chat" in contexts[0]["continuation_input"]
    assert "E1" in contexts[0]["continuation_input"]


@pytest.mark.asyncio
async def test_turn_exhausts_the_configured_attempt_budget(tmp_path):
    attempts: list[int] = []

    async def invoke(context: dict[str, Any], _parameters: dict[str, str]) -> str:
        attempts.append(context["attempt"])
        return "response"

    with pytest.raises(CompletionRetryExhausted) as excinfo:
        await run_agent_turn(
            invoke=invoke,
            ledger=HostRunLedger("run-1", Work.of_ticket("https://example.test/1")),
            execution_context={
                "max_completion_attempts": 2,
            },
        )

    assert attempts == [1, 2]
    assert excinfo.value.attempts == 2


@pytest.mark.parametrize("category", ["rate_limited", "timeout"])
@pytest.mark.parametrize("failure_stage", ["invoke", "completion"])
@pytest.mark.asyncio
async def test_turn_reraises_terminal_failures_without_retrying(
    tmp_path, monkeypatch, failure_stage, category
):
    calls = 0
    terminal_failure = CliAgentExecutionError(
        cli_agent="codex",
        result=CliAgentExecutionResult(
            stdout="",
            stderr=category,
            returncode=75,
            error_category=category,
            error_details={"retry_after_text": "11:44 AM"},
        ),
    )

    def raise_wrapped_failure(*args, **kwargs):
        raise CommandError("wrapped") from terminal_failure

    async def invoke(context: dict[str, Any], _parameters: dict[str, str]) -> str:
        nonlocal calls
        calls += 1
        if failure_stage == "invoke":
            raise_wrapped_failure()
        return "response"

    if failure_stage == "completion":
        monkeypatch.setattr(RunStore, "status", raise_wrapped_failure)

    with pytest.raises(CliAgentExecutionError) as excinfo:
        await run_agent_turn(
            invoke=invoke,
            ledger=HostRunLedger("run-1", Work.of_ticket("https://example.test/1")),
            execution_context={
                "max_completion_attempts": 3,
            },
        )

    assert calls == 1
    assert excinfo.value is terminal_failure


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["process", "protocol", "session_unavailable"])
async def test_other_cli_failures_still_use_the_full_attempt_budget(tmp_path, category):
    attempts = []
    error = CliAgentExecutionError(
        cli_agent="codex",
        result=CliAgentExecutionResult(
            stdout="", stderr="failed", returncode=1, error_category=category
        ),
    )

    async def invoke(context, parameters):
        attempts.append(context["attempt"])
        raise CommandError("wrapped") from error

    with pytest.raises(CompletionRetryExhausted):
        await run_agent_turn(
            invoke=invoke,
            ledger=HostRunLedger("run-1", Work.of_ticket("https://example.test/1")),
            execution_context={"max_completion_attempts": 3},
        )
    assert attempts == [1, 2, 3]


@pytest.mark.asyncio
async def test_turn_records_completion_missing_then_completed_events(
    tmp_path, monkeypatch
):
    completion_call = 2
    max_attempts = 3
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        command_runner,
        "record_workflow_completed",
        lambda **kwargs: recorded.append(("completed", kwargs)),
    )
    monkeypatch.setattr(
        command_runner,
        "record_workflow_completion_missing",
        lambda **kwargs: recorded.append(("missing", kwargs)),
    )
    store = _store(tmp_path)
    calls = 0

    async def invoke(context: dict[str, Any], _parameters: dict[str, str]) -> str:
        nonlocal calls
        calls += 1
        if calls == completion_call:
            _ticket_completion(store, "run-1")
        return "response"

    await run_agent_turn(
        invoke=invoke,
        ledger=HostRunLedger("run-1", Work.of_ticket("https://example.test/1")),
        execution_context={
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
    assert recorded[1][1] == {"run_id": "run-1", "attempt": 2, "status": "done"}


@pytest.mark.asyncio
async def test_turn_does_not_record_completion_missing_for_invoke_failures(
    tmp_path, monkeypatch
):
    recorded: list[str] = []
    monkeypatch.setattr(
        command_runner,
        "record_workflow_completion_missing",
        lambda **kwargs: recorded.append("missing"),
    )

    async def invoke(context: dict[str, Any], _parameters: dict[str, str]) -> str:
        raise RuntimeError("provider turn failed")

    with pytest.raises(CompletionRetryExhausted):
        await run_agent_turn(
            invoke=invoke,
            ledger=HostRunLedger("run-1", Work.of_ticket("https://example.test/1")),
            execution_context={
                "max_completion_attempts": 2,
            },
        )

    assert recorded == []


@pytest.mark.asyncio
async def test_turn_can_leave_provider_failures_to_the_chat_dispatcher(tmp_path):
    _chat_run(_store(tmp_path), "run-chat")
    calls = 0

    async def invoke(context: dict[str, Any], _parameters: dict[str, str]) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        await run_agent_turn(
            invoke=invoke,
            ledger=HostRunLedger("run-chat", Work("chat", "slack:C1:T1")),
            execution_context={
                "max_completion_attempts": 3,
                "retry_invoke_exceptions": False,
            },
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_turn_rereads_chat_evidence_for_every_attempt(tmp_path):
    # A session recreated for the second attempt must see what the first one
    # already did, so the run's evidence is re-read before each attempt.
    store = _store(tmp_path)
    _chat_run(store, "run-chat")
    seen: list[list[dict[str, Any]]] = []

    async def invoke(context: dict[str, Any], parameters: dict[str, str]) -> str:
        seen.append(json.loads(parameters["previous_attempt_evidence"]))
        if len(seen) == 1:
            store.append_evidence("run-chat", "chat_reply", {"message_id": "1.2"})
        else:
            store.complete_run(
                "run-chat",
                "done",
                "replied",
                subject_type="chat",
                subject_id="E1",
                person_id="aiko",
            )
        return "response"

    await run_agent_turn(
        invoke=invoke,
        ledger=HostRunLedger("run-chat", Work("chat", "slack:C1:T1")),
        execution_context={
            "event_id": "E1",
            "max_completion_attempts": 2,
        },
    )

    # The batch membership is bookkeeping, not an action the member took.
    assert [[item["evidence_type"] for item in attempt] for attempt in seen] == [
        [],
        ["chat_reply"],
    ]


@pytest.mark.asyncio
async def test_turn_gives_ticket_turns_no_extra_parameters(tmp_path):
    store = _store(tmp_path)
    parameters: list[dict[str, str]] = []

    async def invoke(context: dict[str, Any], params: dict[str, str]) -> str:
        parameters.append(params)
        _ticket_completion(store, "run-1")
        return "response"

    await run_agent_turn(
        invoke=invoke,
        ledger=HostRunLedger("run-1", Work.of_ticket("https://example.test/1")),
        execution_context={
            "max_completion_attempts": 1,
        },
    )

    assert parameters == [{}]


@pytest.mark.asyncio
async def test_a_run_that_awaits_its_checks_is_complete(tmp_path, monkeypatch):
    """Ending to await the checks finishes the run: no further attempt in the
    dispatch, and the completion says how it ended."""
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        command_runner, "record_workflow_completed", lambda **kw: recorded.append(kw)
    )
    store = _store(tmp_path)
    calls = 0

    async def invoke(context: dict[str, Any], _parameters: dict[str, str]) -> str:
        nonlocal calls
        calls += 1
        store.append_evidence(
            "run-1", "checks_awaited", {"target": {"url": "https://example.test/pr/1"}}
        )
        _ticket_completion(store, "run-1", "awaiting_checks")
        return "response"

    await run_agent_turn(
        invoke=invoke,
        ledger=HostRunLedger("run-1", Work.of_ticket("https://example.test/1")),
        execution_context={"max_completion_attempts": 3},
    )

    assert calls == 1
    assert recorded == [{"run_id": "run-1", "attempt": 1, "status": "awaiting_checks"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work",
    [Work("chat", "slack:U_BOT:C1:100.1"), Work("manual", "run-0")],
)
async def test_a_turn_answering_no_chat_event_is_driven_as_any_work(tmp_path, work):
    """A run the patrol resumed in a chat thread's or a command's work answers
    no chat event: it is driven to completion like a ticket's run, with no
    chat parameters and the generic continuation."""
    store = _store(tmp_path)
    seen: list[tuple[dict[str, Any], dict[str, str]]] = []

    async def invoke(context: dict[str, Any], parameters: dict[str, str]) -> str:
        seen.append((context, parameters))
        if len(seen) == 2:
            _ticket_completion(store, "run-1")
        return "response"

    await run_agent_turn(
        invoke=invoke,
        ledger=HostRunLedger("run-1", work),
        execution_context={"max_completion_attempts": 2},
    )

    assert [parameters for _, parameters in seen] == [{}, {}]
    assert seen[1][0]["continuation_input"] == t(
        "commands.workflows.common.agent_continuation", run_id="run-1"
    )
