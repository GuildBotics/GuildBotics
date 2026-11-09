"""Completion and retry contracts against the host ledger protocol."""

import json
import pytest
from guildbotics.commands.agent_turn import run_agent_turn, CompletionRetryExhausted
from guildbotics.intelligences.agent_runtime.wire import Evidence


class Ledger:
    run_id = "run-1"

    def __init__(self):
        self.complete = False
        self.items = []
        self.completed = []
        self.missing = []

    def require_completion(self):
        if not self.complete:
            raise RuntimeError("not completed")

    def evidence(self):
        return self.items

    def record_completed(self, attempt):
        self.completed.append(attempt)

    def record_completion_missing(self, attempt, maximum, error):
        self.missing.append(attempt)


@pytest.mark.asyncio
async def test_retries_completion_with_fresh_evidence_and_continuation():
    ledger = Ledger()
    seen = []

    async def invoke(context, parameters):
        seen.append((context, json.loads(parameters["previous_attempt_evidence"])))
        ledger.items.append(Evidence(type="chat_batch", payload={}, recorded_at="t"))
        ledger.items.append(
            Evidence(type="pr_comment", payload={"id": len(seen)}, recorded_at="t")
        )
        ledger.complete = len(seen) == 2
        return "reply"

    result = await run_agent_turn(
        invoke=invoke,
        execution_context={"max_completion_attempts": 2, "event_id": "e1"},
        ledger=ledger,
    )
    assert result == "reply"
    assert ledger.completed == [2] and ledger.missing == [1]
    assert seen[0][1] == []
    assert seen[1][1] == [
        {"type": "pr_comment", "payload": {"id": 1}, "recorded_at": "t"}
    ]
    assert seen[1][0]["resume_policy"] == "auto"
    assert seen[1][0]["attempt"] == 2
    assert seen[1][0]["continuation_input"]


@pytest.mark.asyncio
async def test_exhaustion_reports_budget_without_marking_complete():
    ledger = Ledger()

    async def invoke(context, parameters):
        assert parameters == {}
        return "incomplete"

    with pytest.raises(CompletionRetryExhausted) as caught:
        await run_agent_turn(
            invoke=invoke,
            execution_context={"max_completion_attempts": 2},
            ledger=ledger,
        )
    assert caught.value.attempts == 2
    assert ledger.missing == [1, 2] and ledger.completed == []


@pytest.mark.asyncio
async def test_disabled_provider_retry_propagates_failure():
    ledger = Ledger()

    async def invoke(context, parameters):
        raise ValueError("provider failed")

    with pytest.raises(ValueError, match="provider failed"):
        await run_agent_turn(
            invoke=invoke,
            execution_context={
                "max_completion_attempts": 3,
                "retry_invoke_exceptions": False,
            },
            ledger=ledger,
        )
    assert ledger.missing == [] and ledger.completed == []


@pytest.mark.parametrize("category", ["timeout", "rate_limited"])
@pytest.mark.asyncio
async def test_terminal_cli_failures_are_not_retried(category):
    from guildbotics.intelligences.agent_runtime.models import (
        CliAgentExecutionError,
        CliAgentExecutionResult,
    )

    ledger = Ledger()
    error = CliAgentExecutionError(
        cli_agent="codex",
        result=CliAgentExecutionResult(
            stdout="", stderr="failed", returncode=1, error_category=category
        ),
    )
    calls = []

    async def invoke(context, parameters):
        calls.append(context)
        raise error

    with pytest.raises(CliAgentExecutionError) as caught:
        await run_agent_turn(
            invoke=invoke,
            execution_context={"max_completion_attempts": 3},
            ledger=ledger,
        )
    assert caught.value is error
    assert len(calls) == 1 and ledger.completed == []


@pytest.mark.parametrize("category", ["process", "protocol", "session_unavailable"])
@pytest.mark.asyncio
async def test_other_cli_failures_keep_the_attempt_budget_without_completion_events(
    category,
):
    from guildbotics.commands.errors import CommandError
    from guildbotics.intelligences.agent_runtime.models import (
        CliAgentExecutionError,
        CliAgentExecutionResult,
    )

    ledger = Ledger()
    error = CliAgentExecutionError(
        cli_agent="codex",
        result=CliAgentExecutionResult(
            stdout="", stderr="failed", returncode=1, error_category=category
        ),
    )
    attempts = []

    async def invoke(context, parameters):
        attempts.append(context["attempt"])
        raise CommandError("wrapped") from error

    with pytest.raises(CompletionRetryExhausted):
        await run_agent_turn(
            invoke=invoke,
            execution_context={"max_completion_attempts": 3},
            ledger=ledger,
        )
    assert attempts == [1, 2, 3]
    assert ledger.missing == [] and ledger.completed == []


@pytest.mark.parametrize("event_id", ["E1", ""])
@pytest.mark.asyncio
async def test_chat_event_and_non_event_work_complete_with_their_own_parameters(
    event_id,
):
    ledger = Ledger()
    seen = []

    async def invoke(context, parameters):
        seen.append((context, parameters))
        ledger.complete = True
        return "completed"

    assert (
        await run_agent_turn(
            invoke=invoke,
            execution_context={"event_id": event_id, "max_completion_attempts": 2},
            ledger=ledger,
        )
        == "completed"
    )
    assert len(seen) == 1 and ledger.completed == [1]
    assert ledger.run_id in seen[0][0]["continuation_input"]
    if event_id:
        assert event_id in seen[0][0]["continuation_input"]
        assert seen[0][1] == {"previous_attempt_evidence": "[]"}
    else:
        assert seen[0][1] == {}
