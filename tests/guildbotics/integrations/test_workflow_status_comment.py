from datetime import UTC, datetime

from guildbotics.integrations.chat_workflow_status import (
    WORKFLOW_STATUS_KIND,
    WORKFLOW_STATUS_ROUTING_SUPPRESS,
)
from guildbotics.integrations.workflow_status_comment import (
    WORKFLOW_STATUS_CODE_BLOCK,
    WorkflowStatusComment,
    parse_workflow_status_comment,
    render_workflow_status_comment,
    suppresses_ticket_selection,
    workflow_status_comment_payload,
)


def test_workflow_status_comment_payload_builds_correct_dict():
    payload = workflow_status_comment_payload(
        reason="rate_limited",
        person_id="aiko",
        run_id="run-123",
        subject_id="sub-456",
        retry_after_at="2026-07-05T21:37:00+09:00",
        retry_after_text="try again at 9:37 PM",
    )
    assert payload == {
        "kind": WORKFLOW_STATUS_KIND,
        "routing": WORKFLOW_STATUS_ROUTING_SUPPRESS,
        "reason": "rate_limited",
        "person_id": "aiko",
        "run_id": "run-123",
        "subject_id": "sub-456",
        "retry_after_at": "2026-07-05T21:37:00+09:00",
        "retry_after_text": "try again at 9:37 PM",
    }


def test_render_and_parse_workflow_status_comment_round_trip():
    payload = workflow_status_comment_payload(
        reason="rate_limited",
        person_id="aiko",
        run_id="run-123",
        subject_id="sub-456",
        retry_after_at="2026-07-05T21:37:00+09:00",
        retry_after_text="try again at 9:37 PM",
    )
    # The body may contain proxy agent signatures or @mentions.
    body = "@user\n\nSome human readable text.\n\n⚙aiko"
    rendered = render_workflow_status_comment(body=body, payload=payload)

    assert WORKFLOW_STATUS_CODE_BLOCK in rendered
    assert "Some human readable text." in rendered

    parsed = parse_workflow_status_comment(rendered)
    assert parsed == WorkflowStatusComment(
        reason="rate_limited",
        routing=WORKFLOW_STATUS_ROUTING_SUPPRESS,
        person_id="aiko",
        run_id="run-123",
        subject_id="sub-456",
        retry_after_at="2026-07-05T21:37:00+09:00",
        retry_after_text="try again at 9:37 PM",
    )


def test_parse_workflow_status_comment_returns_none_when_no_fenced_block():
    body = "Just a normal comment.\nNo special block here."
    assert parse_workflow_status_comment(body) is None


def test_parse_workflow_status_comment_returns_none_when_broken_json():
    body = f"```{WORKFLOW_STATUS_CODE_BLOCK}\n{{broken: json,\n```"
    assert parse_workflow_status_comment(body) is None


def test_parse_workflow_status_comment_returns_none_when_different_kind():
    payload = workflow_status_comment_payload(
        reason="rate_limited",
        person_id="aiko",
        run_id="run-123",
    )
    payload["kind"] = "other_kind"
    rendered = render_workflow_status_comment(body="body", payload=payload)
    assert parse_workflow_status_comment(rendered) is None


def test_suppresses_ticket_selection_with_rate_limited_and_future_retry_after():
    status = WorkflowStatusComment(
        reason="rate_limited",
        routing=WORKFLOW_STATUS_ROUTING_SUPPRESS,
        person_id="aiko",
        run_id="1",
        retry_after_at="2026-07-05T22:00:00+00:00",
    )
    now = datetime(2026, 7, 5, 21, 0, 0, tzinfo=UTC)
    assert suppresses_ticket_selection(status, now=now) is True


def test_suppresses_ticket_selection_with_rate_limited_and_past_retry_after():
    status = WorkflowStatusComment(
        reason="rate_limited",
        routing=WORKFLOW_STATUS_ROUTING_SUPPRESS,
        person_id="aiko",
        run_id="1",
        retry_after_at="2026-07-05T20:00:00+00:00",
    )
    now = datetime(2026, 7, 5, 21, 0, 0, tzinfo=UTC)
    assert suppresses_ticket_selection(status, now=now) is False


def test_suppresses_ticket_selection_with_rate_limited_and_empty_retry_after():
    status = WorkflowStatusComment(
        reason="rate_limited",
        routing=WORKFLOW_STATUS_ROUTING_SUPPRESS,
        person_id="aiko",
        run_id="1",
        retry_after_at="",
    )
    assert suppresses_ticket_selection(status) is True


def test_suppresses_ticket_selection_with_rate_limited_and_unparseable_retry_after():
    status = WorkflowStatusComment(
        reason="rate_limited",
        routing=WORKFLOW_STATUS_ROUTING_SUPPRESS,
        person_id="aiko",
        run_id="1",
        retry_after_at="not a date",
    )
    assert suppresses_ticket_selection(status) is True


def test_suppresses_ticket_selection_with_rate_limited_and_offset_naive_retry_after():
    status = WorkflowStatusComment(
        reason="rate_limited",
        routing=WORKFLOW_STATUS_ROUTING_SUPPRESS,
        person_id="aiko",
        run_id="1",
        retry_after_at="2026-07-05T22:00:00",  # No timezone
    )
    assert suppresses_ticket_selection(status) is True


def test_suppresses_ticket_selection_with_failed_reason():
    status = WorkflowStatusComment(
        reason="failed",
        routing=WORKFLOW_STATUS_ROUTING_SUPPRESS,
        person_id="aiko",
        run_id="1",
    )
    assert suppresses_ticket_selection(status) is True


def test_suppresses_ticket_selection_returns_false_for_non_suppress_routing():
    status = WorkflowStatusComment(
        reason="failed",
        routing="other",
        person_id="aiko",
        run_id="1",
    )
    assert suppresses_ticket_selection(status) is False


def test_suppresses_ticket_selection_returns_false_for_unknown_reason():
    status = WorkflowStatusComment(
        reason="unknown",
        routing=WORKFLOW_STATUS_ROUTING_SUPPRESS,
        person_id="aiko",
        run_id="1",
    )
    assert suppresses_ticket_selection(status) is False
