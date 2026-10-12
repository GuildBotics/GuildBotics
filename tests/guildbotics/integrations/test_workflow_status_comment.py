import json

import pytest

from guildbotics.integrations.github.workflow_status_comment import (
    WORKFLOW_STATUS_MARKER,
    parse_workflow_status_comment,
    render_workflow_status_comment,
)
from guildbotics.runtime.port import WorkflowStatus

STATUS = WorkflowStatus(
    reason="rate_limited",
    person_id="aiko",
    run_id="run-123",
    subject="https://github.com/acme/demo/issues/7",
    retry_after_at="2026-07-05T21:37:00+09:00",
    retry_after_text="try again at 9:37 PM",
)


def _marker_json(rendered: str) -> dict:
    return json.loads(rendered.split("\n", 2)[1])


def test_the_marker_is_the_status_payload():
    """The marker is the status's own payload: a status-marked JSON object of
    the fields the status has."""
    rendered = render_workflow_status_comment("body", STATUS)

    assert _marker_json(rendered) == {
        "kind": "workflow_error",
        "routing": "suppress",
        "reason": "rate_limited",
        "person_id": "aiko",
        "run_id": "run-123",
        "subject": "https://github.com/acme/demo/issues/7",
        "retry_after_at": "2026-07-05T21:37:00+09:00",
        "retry_after_text": "try again at 9:37 PM",
    }


def test_the_marker_leaves_out_empty_fields():
    rendered = render_workflow_status_comment(
        "body", WorkflowStatus(reason="failed", person_id="aiko", run_id="run-1")
    )

    assert _marker_json(rendered) == {
        "kind": "workflow_error",
        "routing": "suppress",
        "reason": "failed",
        "person_id": "aiko",
        "run_id": "run-1",
    }


def test_a_status_round_trips_through_a_posted_comment():
    # The body may contain agent tokens or @mentions.
    body = "@user\n\nSome human readable text.\n\n⚙aiko"
    rendered = render_workflow_status_comment(body, STATUS)

    assert rendered.startswith(f"<!-- {WORKFLOW_STATUS_MARKER}\n")
    assert rendered.endswith(body)
    assert "```" not in rendered
    # The comment is posted with the author's mention before it.
    assert parse_workflow_status_comment(f"@author\n\n{rendered}") == STATUS


@pytest.mark.parametrize(
    "status",
    [
        WorkflowStatus(reason="failed", person_id="aiko", run_id="run-1"),
        WorkflowStatus(reason="review_limit", person_id="aiko", run_id=""),
    ],
)
def test_every_reason_round_trips(status):
    rendered = render_workflow_status_comment("body", status)

    assert parse_workflow_status_comment(rendered) == status


def test_a_marker_without_a_run_reads_as_one_no_run_made():
    body = (
        f"<!-- {WORKFLOW_STATUS_MARKER}\n"
        '{"kind":"workflow_error","person_id":"aiko","reason":"review_limit",'
        '"routing":"suppress"}\n-->\n\nbody'
    )

    assert parse_workflow_status_comment(body) == WorkflowStatus(
        reason="review_limit", person_id="aiko", run_id=""
    )


def test_an_older_marker_naming_its_subject_id_still_holds_its_status():
    body = (
        f"<!-- {WORKFLOW_STATUS_MARKER}\n"
        '{"kind":"workflow_error","person_id":"aiko","reason":"failed",'
        '"routing":"suppress","run_id":"run-1","subject_id":"sub-1"}\n-->\n\nbody'
    )

    assert parse_workflow_status_comment(body) == WorkflowStatus(
        reason="failed", person_id="aiko", run_id="run-1"
    )


@pytest.mark.parametrize(
    "body",
    [
        "Just a normal comment.\nNo special block here.",
        f"<!-- {WORKFLOW_STATUS_MARKER}\n{{broken: json,\n-->",
        # The old fenced block is not a marker.
        f"```{WORKFLOW_STATUS_MARKER}\n"
        '{"kind":"workflow_error","routing":"suppress","reason":"failed"}\n```',
        # Another kind of marker JSON.
        f"<!-- {WORKFLOW_STATUS_MARKER}\n"
        '{"kind":"other_kind","person_id":"aiko","reason":"failed"}\n-->',
        # A reason no status has.
        f"<!-- {WORKFLOW_STATUS_MARKER}\n"
        '{"kind":"workflow_error","person_id":"aiko","reason":"unknown"}\n-->',
        # No member.
        f"<!-- {WORKFLOW_STATUS_MARKER}\n"
        '{"kind":"workflow_error","reason":"failed"}\n-->',
        f"<!-- {WORKFLOW_STATUS_MARKER}\n[1, 2]\n-->",
    ],
)
def test_a_comment_without_a_valid_marker_carries_no_status(body):
    assert parse_workflow_status_comment(body) is None
