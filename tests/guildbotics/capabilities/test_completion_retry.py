from __future__ import annotations

import pytest

from guildbotics.capabilities.completion_retry import (
    CompletionRetryExhausted,
    find_cli_agent_execution_error,
    run_with_completion_retry,
)
from guildbotics.commands.errors import CommandError
from guildbotics.intelligences.brains.cli_agent import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)


@pytest.mark.asyncio
async def test_returns_on_first_completion():
    attempts: list[int] = []

    async def _invoke(run_id, attempt):
        attempts.append(attempt)

    def _check(run_id):
        return f"done:{run_id}"

    result, run_id = await run_with_completion_retry(
        invoke=_invoke, check_completion=_check, max_attempts=5
    )

    assert attempts == [1]
    assert result == f"done:{run_id}"


@pytest.mark.asyncio
async def test_retries_reuse_a_single_run_id():
    seen_run_ids: list[str] = []
    seen_attempts: list[int] = []

    async def _invoke(run_id, attempt):
        seen_run_ids.append(run_id)
        seen_attempts.append(attempt)

    def _check(run_id):
        # Fail until the third attempt.
        if len(seen_attempts) < 3:
            raise RuntimeError("not completed")
        return "ok"

    result, run_id = await run_with_completion_retry(
        invoke=_invoke, check_completion=_check, max_attempts=5
    )

    assert result == "ok"
    assert seen_attempts == [1, 2, 3]
    # One stable run id is reused across attempts so partial evidence accumulates.
    assert set(seen_run_ids) == {run_id}


@pytest.mark.asyncio
async def test_raises_exhausted_after_budget():
    calls = 0

    async def _invoke(run_id, attempt):
        nonlocal calls
        calls += 1

    def _check(run_id):
        raise RuntimeError("never completes")

    with pytest.raises(CompletionRetryExhausted) as excinfo:
        await run_with_completion_retry(
            invoke=_invoke, check_completion=_check, max_attempts=3
        )

    assert calls == 3
    assert excinfo.value.attempts == 3
    assert isinstance(excinfo.value.last_error, RuntimeError)


@pytest.mark.asyncio
async def test_invoke_failure_counts_as_attempt_and_exhausts():
    calls = 0

    async def _invoke(run_id, attempt):
        nonlocal calls
        calls += 1
        raise RuntimeError("agent exited non-zero")

    def _check(run_id):
        raise AssertionError("check_completion must not run when invoke failed")

    with pytest.raises(CompletionRetryExhausted) as excinfo:
        await run_with_completion_retry(
            invoke=_invoke, check_completion=_check, max_attempts=3
        )

    # A failing agent run is bounded by the budget (not retried forever) and the
    # failure is surfaced for escalation.
    assert calls == 3
    assert excinfo.value.attempts == 3
    assert isinstance(excinfo.value.last_error, RuntimeError)
    assert "non-zero" in str(excinfo.value.last_error)


@pytest.mark.asyncio
async def test_retry_invoke_exceptions_false_reraises_immediately():
    calls = 0

    async def _invoke(run_id, attempt):
        nonlocal calls
        calls += 1
        raise RuntimeError("agent exited non-zero")

    with pytest.raises(RuntimeError, match="non-zero"):
        await run_with_completion_retry(
            invoke=_invoke,
            check_completion=lambda _run_id: "never",
            max_attempts=3,
            retry_invoke_exceptions=False,
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_rate_limit_error_is_reraised_without_retry_attempts():
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

    async def _invoke(run_id, attempt):
        nonlocal calls
        calls += 1
        raise CommandError("wrapped") from rate_limited

    with pytest.raises(CliAgentExecutionError) as excinfo:
        await run_with_completion_retry(
            invoke=_invoke,
            check_completion=lambda _run_id: "never",
            max_attempts=3,
        )

    assert calls == 1
    assert excinfo.value.category == "rate_limited"
    assert (
        find_cli_agent_execution_error(excinfo.value, category="rate_limited")
        is excinfo.value
    )


@pytest.mark.asyncio
async def test_records_completion_missing_then_completed_events(monkeypatch):
    from guildbotics.capabilities import workflow_completion_events

    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        workflow_completion_events,
        "record_workflow_completed",
        lambda **kwargs: recorded.append(("completed", kwargs)),
    )
    monkeypatch.setattr(
        workflow_completion_events,
        "record_workflow_completion_missing",
        lambda **kwargs: recorded.append(("missing", kwargs)),
    )
    checks: list[int] = []

    async def _invoke(run_id, attempt):
        pass

    def _check(run_id):
        checks.append(1)
        if len(checks) < 2:
            raise RuntimeError("Task run was not found.")
        return "ok"

    result, run_id = await run_with_completion_retry(
        invoke=_invoke, check_completion=_check, max_attempts=3
    )

    assert result == "ok"
    # The successful provider turn without completion evidence is an explicit
    # diagnostics event, and the eventual completion is recorded as well.
    assert [name for name, _ in recorded] == ["missing", "completed"]
    missing = recorded[0][1]
    assert missing["run_id"] == run_id
    assert missing["attempt"] == 1
    assert missing["max_attempts"] == 3
    assert "not found" in missing["error"]
    completed = recorded[1][1]
    assert completed["run_id"] == run_id
    assert completed["attempt"] == 2


@pytest.mark.asyncio
async def test_invoke_failure_does_not_record_completion_missing(monkeypatch):
    from guildbotics.capabilities import workflow_completion_events

    recorded: list[str] = []
    monkeypatch.setattr(
        workflow_completion_events,
        "record_workflow_completion_missing",
        lambda **kwargs: recorded.append("missing"),
    )

    async def _invoke(run_id, attempt):
        raise RuntimeError("provider turn failed")

    def _check(run_id):
        raise AssertionError("check must not run after invoke failure")

    with pytest.raises(CompletionRetryExhausted):
        await run_with_completion_retry(
            invoke=_invoke, check_completion=_check, max_attempts=2
        )

    # A failed provider turn already records its own failure; the completion
    # layer only reports turns that succeeded without evidence.
    assert recorded == []
