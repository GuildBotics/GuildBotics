from guildbotics.capabilities.completion_retry import CompletionRetryExhausted
from guildbotics.capabilities.workflow_rate_limits import (
    WorkflowRateLimit,
    record_workflow_rate_limited,
    workflow_rate_limit_from_exception,
)
from guildbotics.intelligences.brains.cli_agent import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)


def _make_rate_limit_error() -> CliAgentExecutionError:
    return CliAgentExecutionError(
        cli_agent="codex",
        result=CliAgentExecutionResult(
            stdout="",
            stderr="rate limit",
            returncode=75,
            error_category="rate_limited",
            error_details={
                "retry_after_at": "2026-07-04T11:44:00+09:00",
                "retry_after_text": "11:44 AM",
            },
        ),
    )


def test_workflow_rate_limit_from_exception_extracts_from_cli_agent_error():
    exc = _make_rate_limit_error()
    rate_limit = workflow_rate_limit_from_exception(exc)

    assert rate_limit is not None
    assert rate_limit.retry_after_at == "2026-07-04T11:44:00+09:00"
    assert rate_limit.retry_after_text == "11:44 AM"


def test_workflow_rate_limit_from_exception_extracts_from_completion_retry_exhausted():
    base_exc = _make_rate_limit_error()
    exc = CompletionRetryExhausted("exhausted", last_error=base_exc)
    rate_limit = workflow_rate_limit_from_exception(exc)

    assert rate_limit is not None
    assert rate_limit.retry_after_at == "2026-07-04T11:44:00+09:00"
    assert rate_limit.retry_after_text == "11:44 AM"


def test_workflow_rate_limit_from_exception_returns_none_for_different_category():
    exc = CliAgentExecutionError(
        cli_agent="codex",
        result=CliAgentExecutionResult(
            stdout="",
            stderr="timeout",
            returncode=75,
            error_category="timeout",
            error_details={},
        ),
    )
    assert workflow_rate_limit_from_exception(exc) is None


def test_workflow_rate_limit_from_exception_returns_none_for_plain_exception():
    assert workflow_rate_limit_from_exception(RuntimeError("fail")) is None


def test_workflow_rate_limit_retry_after_display():
    rl1 = WorkflowRateLimit("2026-07-04", "text")
    assert rl1.retry_after_display == "text"

    rl2 = WorkflowRateLimit("2026-07-04", "")
    assert rl2.retry_after_display == "2026-07-04"

    rl3 = WorkflowRateLimit("", "")
    assert rl3.retry_after_display == ""


def test_record_workflow_rate_limited(monkeypatch):
    recorded_args = None
    recorded_kwargs = None

    def fake_record(*args, **kwargs):
        nonlocal recorded_args, recorded_kwargs
        recorded_args = args
        recorded_kwargs = kwargs

    monkeypatch.setattr(
        "guildbotics.capabilities.workflow_rate_limits.record_correlated_event",
        fake_record,
    )

    rate_limit = WorkflowRateLimit("2026-07-04T11:44:00+09:00", "11:44 AM")

    record_workflow_rate_limited(
        person_id="aiko",
        command="workflows/test_workflow",
        run_id="run-1",
        source_event_id="evt-1",
        subject_id="sub-1",
        retry_after=rate_limit,
        default_source="event_listener",
    )

    assert recorded_kwargs is not None
    assert recorded_kwargs["event_type"] == "workflow.rate_limited"
    assert recorded_kwargs["default_source"] == "event_listener"
    assert recorded_kwargs["person_id"] == "aiko"
    assert recorded_kwargs["command"] == "workflows/test_workflow"
    assert recorded_kwargs["attributes"] == {
        "error.category": "rate_limited",
        "rate_limit.retry_after_at": "2026-07-04T11:44:00+09:00",
        "rate_limit.retry_after_text": "11:44 AM",
    }
    assert recorded_kwargs["payload"] == {
        "category": "rate_limited",
        "retry_after_at": "2026-07-04T11:44:00+09:00",
        "retry_after_text": "11:44 AM",
        "source_event_id": "evt-1",
        "subject_id": "sub-1",
        "run_id": "run-1",
    }
