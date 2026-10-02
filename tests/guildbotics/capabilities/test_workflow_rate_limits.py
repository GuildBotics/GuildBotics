import i18n  # type: ignore
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


@pytest.mark.parametrize(
    ("language", "automatic_retry", "unknown_restart"),
    [
        ("en", "retry automatically at or after this time", "restart time is unknown"),
        ("ja", "この時刻以降に自動再試行します", "再開時刻は不明です"),
    ],
)
def test_workflow_rate_limit_notice_matches_retry_behavior(
    language: str, automatic_retry: str, unknown_restart: str
) -> None:
    previous_locale = i18n.get("locale")
    previous_fallback = i18n.get("fallback")
    try:
        from guildbotics.utils.i18n_tool import set_language

        set_language(language)
        scheduled = workflow_rate_limit_notice_text(
            WorkflowRateLimit("2026-07-04T11:44:00+09:00", "11:44 AM")
        )
        unscheduled = workflow_rate_limit_notice_text(WorkflowRateLimit())
    finally:
        i18n.set("locale", previous_locale)
        i18n.set("fallback", previous_fallback)

    assert "2026-07-04 11:44:00+09:00" in scheduled
    assert "11:44 AM" not in scheduled
    assert automatic_retry in scheduled
    assert unknown_restart in unscheduled
    assert "not be retried automatically" not in scheduled
    assert "自動再試行しません" not in scheduled


@pytest.mark.parametrize("language", ["en", "ja"])
@pytest.mark.parametrize(
    ("at", "hint", "suffix", "display"),
    [
        (
            "2026-10-03T15:30:12+09:00",
            "Resets in 25h57m34s",
            "_with_reset",
            "2026-10-03 15:30:12+09:00",
        ),
        ("2026-10-03T06:30:12Z", "", "_with_reset", "2026-10-03 06:30:12+00:00"),
        ("", "Resets in 25h57m34s", "_with_hint", "Resets in 25h57m34s"),
        ("", "", "", ""),
        ("invalid", "Resets in 1h", "_with_hint", "Resets in 1h"),
        ("invalid", "", "", ""),
    ],
)
def test_notice_selects_reset_information(language, at, hint, suffix, display):
    notice = workflow_rate_limit_notice_text
    previous_locale = i18n.get("locale")
    previous_fallback = i18n.get("fallback")
    try:
        from guildbotics.utils.i18n_tool import set_language

        set_language(language)
        key = f"commands.workflows.common.rate_limited_escalation{suffix}"
        expected = t(key, retry_after=display)
        assert expected != key
        assert notice(WorkflowRateLimit(at, hint)) == expected
        if suffix != "_with_reset":
            assert (
                "再開時刻は不明です" if language == "ja" else "restart time is unknown"
            ) in expected
    finally:
        i18n.set("locale", previous_locale)
        i18n.set("fallback", previous_fallback)


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
