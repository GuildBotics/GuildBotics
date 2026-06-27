from __future__ import annotations

import pytest

from guildbotics.capabilities.completion_retry import (
    CompletionRetryExhausted,
    run_with_completion_retry,
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
