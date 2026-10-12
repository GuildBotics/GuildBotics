from datetime import UTC, datetime

import pytest

from guildbotics.runtime.port import WorkflowStatus

NOW = datetime(2026, 7, 5, 21, 0, 0, tzinfo=UTC)


def _status(reason: str, retry_after_at: str = "") -> WorkflowStatus:
    return WorkflowStatus.model_validate(
        {
            "reason": reason,
            "person_id": "aiko",
            "run_id": "run-1",
            "retry_after_at": retry_after_at,
        }
    )


def test_a_failure_holds_the_work_out_of_selection():
    assert _status("failed").holds_selection(NOW) is True


@pytest.mark.parametrize(
    ("retry_after_at", "held"),
    [
        ("2026-07-05T22:00:00+00:00", True),
        # The same instant in another offset is still ahead.
        ("2026-07-06T06:30:00+09:00", True),
        ("2026-07-05T20:00:00+00:00", False),
        ("2026-07-05T21:00:00+00:00", False),
        # A reset that is not known holds it for good.
        ("", True),
        ("not a date", True),
        ("2026-07-05T22:00:00", True),
    ],
)
def test_a_rate_limit_holds_the_work_until_its_reset(retry_after_at, held):
    assert _status("rate_limited", retry_after_at).holds_selection(NOW) is held


def test_a_rate_limit_is_compared_with_the_present_by_default():
    assert _status("rate_limited", "2000-01-01T00:00:00+00:00").holds_selection() is (
        False
    )
    assert _status("rate_limited", "2999-01-01T00:00:00+00:00").holds_selection() is (
        True
    )


def test_the_review_limit_does_not_hold_the_work():
    assert _status("review_limit").holds_selection(NOW) is False


def test_a_status_has_a_known_reason_and_nothing_else():
    with pytest.raises(ValueError):
        _status("unknown")
    with pytest.raises(ValueError):
        WorkflowStatus.model_validate(
            {"reason": "failed", "person_id": "aiko", "run_id": "", "routing": "x"}
        )


def test_a_status_payload_marks_the_fields_it_has():
    status = WorkflowStatus(
        reason="rate_limited",
        person_id="aiko",
        run_id="run-1",
        subject="https://github.com/acme/demo/issues/7",
        retry_after_at="2026-07-05T22:00:00+00:00",
    )

    assert status.payload() == {
        "kind": "workflow_error",
        "routing": "suppress",
        "reason": "rate_limited",
        "person_id": "aiko",
        "run_id": "run-1",
        "subject": "https://github.com/acme/demo/issues/7",
        "retry_after_at": "2026-07-05T22:00:00+00:00",
    }
    assert WorkflowStatus.from_payload(status.payload()) == status


def test_a_payload_reads_only_the_fields_a_status_has():
    """An older carrier's extra keys (``subject_id``) are left out, and a
    payload without a run reads as a notice no run made."""
    payload = {
        "kind": "workflow_error",
        "routing": "suppress",
        "reason": "review_limit",
        "person_id": "aiko",
        "subject_id": "sub-1",
    }

    assert WorkflowStatus.from_payload(payload) == WorkflowStatus(
        reason="review_limit", person_id="aiko", run_id=""
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        ["workflow_error"],
        {"reason": "failed", "person_id": "aiko", "run_id": "1"},
        {"kind": "other", "reason": "failed", "person_id": "aiko", "run_id": "1"},
        {"kind": "workflow_error", "reason": "unknown", "person_id": "aiko"},
        {"kind": "workflow_error", "reason": "failed"},
        {"kind": "workflow_error", "reason": "failed", "person_id": 7},
    ],
)
def test_a_payload_that_is_no_status_reads_as_none(payload):
    assert WorkflowStatus.from_payload(payload) is None
