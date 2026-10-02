import pytest

from guildbotics.capabilities.workflow_rate_limits import (
    WorkflowRateLimit,
    record_workflow_rate_limited,
    workflow_rate_limit_from_exception,
    workflow_rate_limit_notice_text,
)
from guildbotics.commands.agent_turn import CompletionRetryExhausted
from guildbotics.intelligences.brains.cli_agent import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)
from guildbotics.utils.i18n_tool import t


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


def test_workflow_rate_limit_from_exception_extracts_from_exhausted_turn_last_error():
    base_exc = _make_rate_limit_error()
    exc = CompletionRetryExhausted(1, last_error=base_exc)
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


@pytest.mark.parametrize("notice_language", ["en", "ja"], indirect=True)
@pytest.mark.parametrize("workflow", ["ticket", "chat"])
@pytest.mark.parametrize(
    "at, hint", [("2026-07-04T02:44:00Z", "11:44 AM"), ("", "Resets in 1h"), ("", "")]
)
def test_notice_explains_the_workflow_retry(
    notice_language, workflow, at, hint, local_rate_limit_timezone
):
    notice = workflow_rate_limit_notice_text(
        WorkflowRateLimit(at, hint), workflow=workflow
    )
    if workflow == "chat":
        guidance = t("commands.workflows.common.rate_limited_retry_chat")
        assert (
            "再試行回数が残っていれば"
            if notice_language == "ja"
            else "if attempts remain"
        ) in guidance
    elif at:
        guidance = t("commands.workflows.common.rate_limited_retry_at")
        assert (
            "この時刻以降に自動再試行します"
            if notice_language == "ja"
            else "retry automatically at or after this time"
        ) in guidance
    else:
        guidance = t("commands.workflows.common.rate_limited_retry_ticket")
        assert (
            "新しいコメントが付くまで"
            if notice_language == "ja"
            else "until a new comment is added"
        ) in guidance
    assert guidance in notice
    if at:
        assert "2026-07-04 11:44:00+09:00" in notice
        assert hint not in notice
    else:
        assert hint in notice
        assert (
            "復帰時刻は不明です" if notice_language == "ja" else "reset time is unknown"
        ) in notice


@pytest.mark.parametrize("notice_language", ["en", "ja"], indirect=True)
@pytest.mark.parametrize(
    ("at", "hint", "suffix", "display"),
    [
        (
            "2026-10-03T15:30:12+09:00",
            "Resets in 25h57m34s",
            "_with_reset",
            "2026-10-03 15:30:12+09:00",
        ),
        ("2026-10-03T06:30:12Z", "", "_with_reset", "2026-10-03 15:30:12+09:00"),
        ("", "Resets in 25h57m34s", "_with_hint", "Resets in 25h57m34s"),
        ("", "", "", ""),
        ("invalid", "Resets in 1h", "_with_hint", "Resets in 1h"),
        ("invalid", "", "", ""),
        ("2026-10-03T06:30:12", "Resets in 1h", "_with_hint", "Resets in 1h"),
        ("2026-10-03T06:30:12", "", "", ""),
    ],
)
def test_notice_selects_reset_information(
    notice_language, at, hint, suffix, display, local_rate_limit_timezone
):
    key = f"commands.workflows.common.rate_limited_escalation{suffix}"
    expected = t(
        key,
        retry_after=display,
        retry_guidance=t("commands.workflows.common.rate_limited_retry_chat"),
    )
    assert expected != key
    assert (
        workflow_rate_limit_notice_text(WorkflowRateLimit(at, hint), workflow="chat")
        == expected
    )
    assert display in expected
    if suffix != "_with_reset":
        assert (
            "復帰時刻は不明です" if notice_language == "ja" else "reset time is unknown"
        ) in expected


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
