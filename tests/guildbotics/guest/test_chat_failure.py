"""The chat host sees the same structured CLI failure through every wrapper."""

import pytest

from guildbotics.commands.agent_turn import CompletionRetryExhausted
from guildbotics.guest.entry import _failure
from guildbotics.intelligences.agent_runtime.models import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("category", ["rate_limited", "timeout"])
def test_direct_and_exhausted_cli_failures_preserve_category_and_reset(
    wrapped, category
):
    original = CliAgentExecutionError(
        cli_agent="codex",
        result=CliAgentExecutionResult(
            stdout="",
            stderr="failed",
            returncode=75,
            error_category=category,
            error_details={
                "retry_after_at": "2026-07-04T11:44:00+09:00",
                "retry_after_text": "11:44 AM",
            },
        ),
    )
    error = CompletionRetryExhausted(1, last_error=original) if wrapped else original
    failure = _failure(error)
    assert failure.type == (
        "CompletionRetryExhausted" if wrapped else "CliAgentExecutionError"
    )
    assert failure.cli_agent == "codex"
    assert failure.cli_agent_result["error_category"] == category
    assert failure.cli_agent_result["error_details"] == original.result.error_details


def test_a_plain_exception_carries_no_cli_rate_limit():
    failure = _failure(RuntimeError("plain"))
    assert failure.cli_agent_result is None
    assert failure.cli_agent == ""
